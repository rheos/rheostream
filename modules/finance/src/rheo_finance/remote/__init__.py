"""The laptop reaches the module over SSH (ADR-0026).

``frames`` is the stream grammar and the path rule, ``receive`` the all-or-nothing
receiver, ``send`` what a push or pull sends, and ``ssh`` the one module that starts a
process. ``cli.remote`` holds the commands and the two server halves.

None of these modules imports sqlalchemy, ``rheo_finance.storage``,
``rheo_finance.wiring`` or ``rheo_core``: the laptop half runs without a database.
"""
