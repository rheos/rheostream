"""The lexical query builder: a caller's words to the ``tsquery`` recall matches with.

Ported from the predecessor's ``ftsQuery()`` (its issue #37), decision logic only.
``plainto_tsquery`` AND-joins every lexeme, so a natural-language question missing one
content word from the memory it is about returns nothing. This builder OR-joins
instead, and drops the lexemes too common to discriminate:

1. **Normalise** with ``to_tsvector('english', ...)``, the configuration that built
   ``search_tsv``. Stopwording, stemming, separators and escaping are Postgres's, so
   none of the predecessor's hand-rolled versions of them is ported.
2. **Gate**: fewer than :data:`LEXICAL_MIN_TERMS_FOR_DF` lexemes are not filtered.
3. **Filter**: drop lexemes at or above :data:`LEXICAL_DF_THRESHOLD` document
   frequency; a lexeme the statistics do not know is rare and kept. If nothing
   survives, keep the :data:`LEXICAL_RAREST_KEPT` rarest instead of an empty query.
4. **Assemble**: ``quote_literal`` each survivor, join with `` | ``, and cast the
   string to ``tsquery``. Not ``to_tsquery``: that re-parses a compound lexeme such as
   ``widget-intak`` into a phrase over its parts, bringing back the parts step 3 may
   have dropped, where the cast keeps it one lexeme.

A caller's double-quoted ``"phrase"`` is the exception: it goes through
``phraseto_tsquery``, is OR-ed in, and never reaches step 3.
"""

import re
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Connection,
    cast,
    func,
    literal,
    literal_column,
    select,
)
from sqlalchemy.dialects.postgresql import TSQUERY

from rheo_recallatron.configuration import (
    LEXICAL_DF_THRESHOLD,
    LEXICAL_MIN_TERMS_FOR_DF,
    LEXICAL_RAREST_KEPT,
)
from rheo_recallatron.storage.repository import lexeme_document_frequencies

SEARCH_CONFIG: Final[ColumnElement[Any]] = literal_column("'english'::regconfig")
"""The text-search configuration every part of the query uses.

It has to be the one the stored generated column was built with — see the
``search_tsv`` column in this package's tables module — or the query would be matched
against lexemes produced by a different dictionary. Cast explicitly for the same reason
the generated column casts: the one-argument forms read a session setting and are only
``STABLE``.
"""

_QUOTED_PHRASE: Final = re.compile(r'"([^"]+)"')


def _content_lexemes(conn: Connection, words: str) -> list[tuple[str, str]]:
    """Each content lexeme of ``words`` with its ``quote_literal`` form, in lexeme
    order."""
    lexeme = func.unnest(
        func.tsvector_to_array(func.to_tsvector(SEARCH_CONFIG, words))
    ).column_valued("lexeme")
    statement = select(lexeme, func.quote_literal(lexeme)).order_by(lexeme)
    return [
        (str(plain), str(quoted)) for plain, quoted in conn.execute(statement).all()
    ]


def _surviving(
    conn: Connection, lexemes: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Steps 2 and 3: the lexemes the document-frequency filter keeps."""
    if len(lexemes) < LEXICAL_MIN_TERMS_FOR_DF:
        return lexemes
    frequencies = lexeme_document_frequencies(conn, [plain for plain, _ in lexemes])

    def frequency(item: tuple[str, str]) -> float:
        # Unknown to the statistics means rare: the safe direction keeps the term.
        return frequencies.get(item[0], 0.0)

    kept = [item for item in lexemes if frequency(item) < LEXICAL_DF_THRESHOLD]
    if kept:
        return kept
    return sorted(lexemes, key=lambda item: (frequency(item), item[0]))[
        :LEXICAL_RAREST_KEPT
    ]


def lexical_tsquery(conn: Connection, query: str) -> ColumnElement[Any]:
    """The ``tsquery`` expression recall matches ``search_tsv`` against.

    Runs the normalisation (and, from three lexemes, the frequency lookup) on ``conn``
    now, and returns an expression for the caller's statement. A query with no content
    lexemes and no phrase is an empty ``tsquery`` that matches nothing, exactly what
    ``plainto_tsquery`` gives for the same input.
    """
    phrases = [
        phrase.strip() for phrase in _QUOTED_PHRASE.findall(query) if phrase.strip()
    ]
    words = _QUOTED_PHRASE.sub(" ", query)
    survivors = _surviving(conn, _content_lexemes(conn, words))
    combined: ColumnElement[Any] = cast(
        literal(" | ".join(quoted for _, quoted in survivors)), TSQUERY
    )
    for phrase in phrases:
        combined = combined.op("||")(func.phraseto_tsquery(SEARCH_CONFIG, phrase))
    return combined
