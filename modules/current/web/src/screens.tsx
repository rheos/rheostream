import type { ReactNode } from "react";
import type {
  OperationOutcome,
  ScreenProps,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import {
  ActionButton,
  ActionForm,
  LocalTime,
  ProjectPicker,
  StatusActions,
  type Field,
  type PickableProject,
  type FormProps,
} from "./forms";
import {
  PRIORITIES,
  PRIORITY_LABEL,
  PROVENANCE_LABEL,
  STATUS_LABEL,
  age,
  area as areaOf,
  areaLook,
  areaLabel,
  areaLabels,
  author,
  day,
  itemDetail,
  itemSummary,
  page,
  pageOffset,
  project as projectOf,
  read,
  summary,
  type Area,
  type ItemSummary,
  type Neighbour,
  type Project,
} from "./data";
import { ChipMenu, DeskRow, WokeSection, type DeskRowView } from "./desk";
import { Icon } from "./icons";
import styles from "./current.module.css";

const PAGE = 25;
const NAV = [
  ["queue", "Queue"],
  ["projects", "Projects"],
  ["areas", "Areas"],
] as const;

function Frame({
  shell,
  active,
  title,
  description,
  action = true,
  children,
}: {
  shell: ShellApi;
  active: string;
  title: string;
  description?: ReactNode;
  action?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={styles.surface}>
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h1 className={styles.heading}>{title}</h1>
          {description ? <p className={styles.intro}>{description}</p> : null}
        </div>
        {action && shell.submit ? (
          <a className={styles.primary} href={shell.href("new")}>
            New item
          </a>
        ) : null}
      </header>
      <nav className={styles.nav} aria-label="Current">
        {NAV.filter(([id]) => id !== "areas" || shell.role === "owner").map(
          ([id, text]) => (
            <a
              key={id}
              href={shell.href(id)}
              aria-current={active === id ? "page" : undefined}
            >
              {text}
            </a>
          ),
        )}
      </nav>
      {children}
    </section>
  );
}
function Failure({ outcome }: { outcome: OperationOutcome }) {
  return (
    <div className={styles.empty} role="alert">
      <h2>
        {outcome.state === "refused" && outcome.code === "not_found"
          ? "Record unavailable"
          : "Could not load this view"}
      </h2>
      <p>
        {outcome.state === "refused"
          ? outcome.code === "not_found"
            ? "It may have been deleted, or the link is out of date."
            : outcome.text || "Your access may have changed."
          : "The service did not respond. Reload to try again."}
      </p>
    </div>
  );
}
function Form({
  shell,
  ...props
}: Omit<FormProps, "submit"> & { shell: ShellApi }) {
  return shell.submit ? (
    <ActionForm {...props} submit={shell.submit} />
  ) : (
    <p className={styles.meta}>Editing is unavailable in this view.</p>
  );
}

// --- shared pieces ----------------------------------------------------------------

function areaOptions(
  areas: readonly Area[],
  held: string | null,
): { value: string; label: string }[] {
  // Live areas, plus a retired one the record already holds so it stays selected.
  return [
    { value: "", label: "No area" },
    ...areas
      .filter((a) => !a.retired || a.key === held)
      .map((a) => ({
        value: a.key,
        label: a.retired ? `${a.label} (retired)` : a.label,
      })),
  ];
}
function projectOptions(
  projects: readonly Project[],
  held: string | null,
): { value: string; label: string }[] {
  return [
    { value: "", label: "No project" },
    ...projects
      .filter((p) => p.status !== "done" || p.ref === held)
      .map((p) => ({
        value: p.ref,
        label: p.status === "active" ? p.title : `${p.title} (${p.status})`,
      })),
  ];
}
/** Only what the project picker shows, so descriptions stay on the server. */
function pickable(projects: readonly Project[]): PickableProject[] {
  return projects.map(({ ref, title, status }) => ({ ref, title, status }));
}
const priorityOptions = PRIORITIES.map((p) => ({
  value: p,
  label: PRIORITY_LABEL[p],
}));

function PriorityMark({ priority }: { priority: ItemSummary["priority"] }) {
  return (
    <span className={`${styles.priority} ${styles[priority] ?? ""}`}>
      {PRIORITY_LABEL[priority]}
    </span>
  );
}
function Pager({
  shell,
  route,
  query,
  offset,
  size = PAGE,
  shown,
  nextOffset,
}: {
  shell: ShellApi;
  route: string;
  query: ScreenProps["query"];
  offset: number;
  size?: number;
  shown: number;
  nextOffset: number | null;
}) {
  if (!offset && nextOffset === null) return null;
  return (
    <nav className={styles.pagination} aria-label="Pages">
      {offset ? (
        <a
          className={styles.secondary}
          href={shell.href(route, {
            ...query,
            offset: String(Math.max(0, offset - size)),
          })}
        >
          Previous page
        </a>
      ) : (
        <span />
      )}
      <span className={styles.meta}>
        Items {offset + 1} to {offset + shown}
      </span>
      {nextOffset !== null ? (
        <a
          className={styles.secondary}
          href={shell.href(route, { ...query, offset: String(nextOffset) })}
        >
          Next page
        </a>
      ) : (
        <span />
      )}
    </nav>
  );
}

// --- queue ------------------------------------------------------------------------

const LIST_STATUSES = [
  ["active", "In the queue"],
  ["open", "Open"],
  ["waiting", "Watching"],
  ["snoozed", "Snoozed"],
  ["done", "Done"],
  ["all", "All"],
] as const;
type ListStatus = (typeof LIST_STATUSES)[number][0];
const QUEUE_PAGE = 50;

/** The list filters a queue URL carries, bounded to what the operation accepts. */
export function queueFilters(query: ScreenProps["query"]) {
  const status: ListStatus =
    LIST_STATUSES.find(([value]) => value === query.status)?.[0] ?? "active";
  const pick = (value: string | undefined, pattern: RegExp) =>
    value && pattern.test(value) ? value : null;
  return {
    status,
    query: (query.q ?? "").trim().slice(0, 512) || null,
    area: pick(query.area, /^[a-z][a-z0-9_]{0,39}$/),
    project_ref: pick(query.project, /^current\.project:[0-9a-f-]{36}$/),
    priority: (PRIORITIES as readonly string[]).includes(query.priority ?? "")
      ? (query.priority ?? null)
      : null,
    overdue: query.overdue === "1" ? true : null,
    blocked: query.blocked === "1" ? true : null,
  };
}

/** A calendar date as "Jan 5", with the year only when it is not this one. */
function shortDay(value: string | null, now: Date): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "";
  const d = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(d.valueOf())) return "";
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    year: d.getUTCFullYear() === now.getUTCFullYear() ? undefined : "numeric",
    timeZone: "UTC",
  }).format(d);
}
function untilText(iso: string, zone: string | undefined): string {
  const d = new Date(iso);
  if (Number.isNaN(d.valueOf())) return "";
  const text = new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: zone ?? "UTC",
  }).format(d);
  return `Until ${text}${zone ? "" : " UTC"}`;
}

/** Everything a desk row shows, worked out here so the client gets text only. */
export function deskRow(
  shell: ShellApi,
  item: ItemSummary,
  labels: ReadonlyMap<string, string>,
  now: Date,
): DeskRowView {
  const look = areaLook(item.area);
  return {
    ref: item.ref,
    title: item.title,
    status: item.status,
    priority: item.priority,
    revision: item.revision,
    href: shell.href("item", { ref: item.ref }),
    areaName: areaLabel(labels, item.area),
    areaIcon: look.icon,
    areaTone: look.tone,
    tag: item.tag,
    due: item.dueOn ? `Due ${shortDay(item.dueOn, now)}` : "",
    overdue: item.overdue,
    blocked: item.blocked,
    until:
      item.status === "snoozed" && item.snoozedUntil && !item.woke
        ? untilText(item.snoozedUntil, shell.timeZone)
        : "",
    signals: item.eventCount,
    age: age(item.updatedAt, now),
    projectRef: item.projectRef,
  };
}

function DeskRows({
  shell,
  items,
  labels,
  label,
  pick,
  reloadHref,
}: {
  shell: ShellApi;
  items: readonly ItemSummary[];
  labels: ReadonlyMap<string, string>;
  label: string;
  pick?: readonly PickableProject[];
  reloadHref?: string;
}) {
  const now = new Date();
  return (
    <ul className={styles.desk} aria-label={label}>
      {items.map((item) => (
        <DeskRow
          key={item.ref}
          row={deskRow(shell, item, labels, now)}
          submit={shell.submit}
          zone={shell.timeZone}
          projects={pick}
          reloadHref={reloadHref}
        />
      ))}
    </ul>
  );
}

type QueueQuery = Record<string, string | undefined>;
/** The queue's URL with one filter changed and paging reset. */
function withFilter(
  shell: ShellApi,
  query: ScreenProps["query"],
  change: QueueQuery,
): string {
  const next: QueueQuery = { ...query, ...change, offset: undefined };
  for (const [key, value] of Object.entries(next))
    if (value === undefined || value === "") delete next[key];
  return shell.href("queue", next);
}

export async function queue({ shell, query }: ScreenProps) {
  const filters = queueFilters(query);
  const offset = pageOffset(query.offset);
  // Expired snoozes get their own section on the first page of the active list.
  const splitWoke = filters.status === "active";
  const [summaryResult, areaResult, projectResult, listResult, wokeResult] =
    await Promise.all([
      read(shell, "current.summary.get"),
      read(shell, "current.area.list", { include_retired: true }),
      read(shell, "current.project.list"),
      read(shell, "current.item.list", {
        ...filters,
        woke: splitWoke ? false : null,
        limit: QUEUE_PAGE,
        offset,
      }),
      splitWoke && offset === 0
        ? read(shell, "current.item.list", {
            ...filters,
            woke: true,
            limit: 50,
          })
        : Promise.resolve(null),
    ]);
  const areas = page(areaResult, areaOf).items;
  const labels = areaLabels(areas);
  const projects = page(projectResult, projectOf).items;
  const list = page(listResult, itemSummary);
  const woke = page(wokeResult, itemSummary);
  const counts =
    summaryResult.state === "ok" ? summary(summaryResult.result) : null;
  const filtered =
    filters.status !== "active" ||
    Boolean(
      filters.query ||
        filters.area ||
        filters.project_ref ||
        filters.priority ||
        filters.overdue ||
        filters.blocked,
    );
  const rowsProps = {
    shell,
    labels,
    pick: projectResult.state === "ok" ? pickable(projects) : undefined,
    reloadHref: shell.href("queue", query),
  };
  const statusLabel =
    filters.status === "active"
      ? null
      : (LIST_STATUSES.find(([v]) => v === filters.status)?.[1] ?? null);
  const areaName = filters.area ? areaLabel(labels, filters.area) : null;
  const projectTitle = filters.project_ref
    ? (projects.find((p) => p.ref === filters.project_ref)?.title ?? "Project")
    : null;
  // Hidden fields carry the other filters through a search.
  const carried = Object.entries({
    status: filters.status === "active" ? undefined : filters.status,
    area: filters.area ?? undefined,
    project: filters.project_ref ?? undefined,
    priority: filters.priority ?? undefined,
    overdue: filters.overdue ? "1" : undefined,
    blocked: filters.blocked ? "1" : undefined,
  }).filter((e): e is [string, string] => e[1] !== undefined);
  return (
    <section className={`${styles.surface} ${styles.deskSurface}`}>
      <h1 className={styles.visuallyHidden}>Queue</h1>
      <div className={styles.deskBar}>
        <form
          action={shell.href("queue")}
          method="get"
          className={styles.finder}
          aria-label="Find items"
        >
          <Icon name="search" className={styles.finderIcon} />
          <label htmlFor="current-q" className={styles.visuallyHidden}>
            Words in titles, notes or comments
          </label>
          <input
            id="current-q"
            name="q"
            defaultValue={query.q}
            maxLength={512}
            enterKeyHint="go"
            autoComplete="off"
            placeholder="Find items…"
          />
          {carried.map(([name, value]) => (
            <input key={name} type="hidden" name={name} value={value} />
          ))}
        </form>
        <a
          className={styles.barIcon}
          href={shell.href("projects")}
          aria-label="Projects"
          title="Projects"
        >
          <Icon name="folder" />
        </a>
        {shell.submit ? (
          <a
            className={styles.barAdd}
            href={shell.href("new")}
            aria-label="New item"
            title="New item"
          >
            <Icon name="plus" />
          </a>
        ) : null}
      </div>
      <nav className={styles.chips} aria-label="Filter the queue">
        <ChipMenu
          label="Status"
          icon="layers"
          value={statusLabel}
          options={LIST_STATUSES.map(([value, text]) => ({
            label: text,
            href: withFilter(shell, query, {
              status: value === "active" ? undefined : value,
            }),
            current: filters.status === value,
          }))}
        />
        <ChipMenu
          label="Area"
          icon="grid"
          value={areaName}
          options={[
            {
              label: "Any area",
              href: withFilter(shell, query, { area: undefined }),
              current: !filters.area,
            },
            ...areas
              .filter((a) => !a.retired || a.key === filters.area)
              .map((a) => ({
                label: a.label,
                href: withFilter(shell, query, { area: a.key }),
                current: filters.area === a.key,
                tone: areaLook(a.key).tone,
              })),
          ]}
        />
        <ChipMenu
          label="Priority"
          icon="flag"
          value={filters.priority ? PRIORITY_LABEL[filters.priority as ItemSummary["priority"]] : null}
          options={[
            {
              label: "Any priority",
              href: withFilter(shell, query, { priority: undefined }),
              current: !filters.priority,
            },
            ...PRIORITIES.map((p) => ({
              label: PRIORITY_LABEL[p],
              href: withFilter(shell, query, { priority: p }),
              current: filters.priority === p,
              priority: p,
            })),
          ]}
        />
        {counts && (counts.overdue || filters.overdue) ? (
          <a
            className={styles.chip}
            data-late="true"
            data-active={filters.overdue || undefined}
            aria-current={filters.overdue ? "true" : undefined}
            href={withFilter(shell, query, {
              overdue: filters.overdue ? undefined : "1",
            })}
          >
            <Icon name="late" />
            Overdue <span className={styles.chipCount}>{counts.overdue}</span>
          </a>
        ) : null}
        {counts && (counts.blocked || filters.blocked) ? (
          <a
            className={styles.chip}
            data-active={filters.blocked || undefined}
            aria-current={filters.blocked ? "true" : undefined}
            href={withFilter(shell, query, {
              blocked: filters.blocked ? undefined : "1",
            })}
          >
            <Icon name="blocked" />
            Blocked <span className={styles.chipCount}>{counts.blocked}</span>
          </a>
        ) : null}
        {projectTitle ? (
          <a
            className={styles.chip}
            data-active="true"
            href={withFilter(shell, query, { project: undefined })}
            aria-label={`Remove the project filter: ${projectTitle}`}
          >
            <Icon name="folder" />
            <span className={styles.chipText}>{projectTitle}</span>
            <Icon name="close" className={styles.chipChevron} />
          </a>
        ) : null}
        {filtered ? (
          <a className={styles.chipClear} href={shell.href("queue")}>
            Clear
          </a>
        ) : null}
      </nav>
      {listResult.state !== "ok" ? (
        <Failure outcome={listResult} />
      ) : (
        <>
          {woke.items.length ? (
            <WokeSection
              items={woke.items.map(({ ref, revision }) => ({ ref, revision }))}
              submit={shell.submit}
            >
              <DeskRows {...rowsProps} items={woke.items} label="Woke up" />
              {woke.nextOffset !== null ? (
                <p className={styles.deskFoot}>
                  More woke items are waiting. Finish or snooze these to see them.
                </p>
              ) : null}
            </WokeSection>
          ) : null}
          {list.items.length ? (
            <DeskRows
              {...rowsProps}
              items={list.items}
              label={splitWoke ? "In the queue" : "Items"}
            />
          ) : woke.items.length ? null : (
            <div className={styles.empty}>
              <h2>{filtered ? "No items match" : "Nothing in the queue"}</h2>
              <p>
                {filtered
                  ? "Try other words or fewer filters."
                  : "Add an item with +, or ask a connected agent to add one."}
              </p>
              {filtered ? (
                <a className={styles.secondary} href={shell.href("queue")}>
                  Clear filters
                </a>
              ) : null}
            </div>
          )}
          <Pager
            shell={shell}
            route="queue"
            query={query}
            offset={offset}
            size={QUEUE_PAGE}
            shown={list.items.length}
            nextOffset={list.nextOffset}
          />
        </>
      )}
    </section>
  );
}

// --- item detail ------------------------------------------------------------------

function NeighbourList({
  shell,
  items,
  empty,
  remove,
}: {
  shell: ShellApi;
  items: readonly Neighbour[];
  empty: string;
  remove: (other: Neighbour) => Record<string, unknown>;
}) {
  if (!items.length) return <p className={styles.meta}>{empty}</p>;
  return (
    <ul className={styles.links}>
      {items.map((other) => (
        <li key={other.ref}>
          <span className={styles.linkText}>
            <a href={shell.href("item", { ref: other.ref })}>{other.title}</a>
            <span className={styles.meta}>{STATUS_LABEL[other.status]}</span>
          </span>
          {shell.submit ? (
            <ActionButton
              submit={shell.submit}
              operation="current.dependency.remove"
              input={remove(other)}
              label="Remove"
              description={`Remove the link to ${other.title}`}
            />
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export async function item({ shell, query }: ScreenProps) {
  if (!query.ref)
    return (
      <Frame shell={shell} active="item" title="Item">
        <div className={styles.empty}>
          <h2>No item selected</h2>
          <p>
            <a href={shell.href("queue")}>Back to the queue</a>
          </p>
        </div>
      </Frame>
    );
  const [result, areaResult, projectResult, candidateResult] =
    await Promise.all([
      read(shell, "current.item.get", { ref: query.ref }),
      read(shell, "current.area.list", { include_retired: true }),
      read(shell, "current.project.list"),
      read(shell, "current.item.list", { status: "active", limit: 100 }),
    ]);
  if (result.state !== "ok")
    return (
      <Frame shell={shell} active="item" title="Item">
        <Failure outcome={result} />
        <p>
          <a href={shell.href("queue")}>Back to the queue</a>
        </p>
      </Frame>
    );
  const record = itemDetail(result.result);
  const areas = page(areaResult, areaOf).items;
  const labels = areaLabels(areas);
  const projects = page(projectResult, projectOf).items;
  const here = shell.href("item", { ref: record.ref });
  const linked = new Set([
    record.ref,
    ...record.blockers.map((b) => b.ref),
    ...record.blocking.map((b) => b.ref),
  ]);
  const candidates = page(candidateResult, itemSummary)
    .items.filter((c) => !linked.has(c.ref))
    .map((c) => ({ value: c.ref, label: c.title }));
  // An explicit empty choice, so a stray click cannot link the first item.
  const choices = [{ value: "", label: "Choose an item" }, ...candidates];
  const base = { ref: record.ref, revision: record.revision };
  const editFields: Field[] = [
    {
      name: "title",
      label: "Title",
      value: record.title,
      required: true,
      maxLength: 500,
    },
    {
      name: "body",
      label: "Notes",
      value: record.body,
      type: "textarea",
      maxLength: 20000,
    },
    {
      name: "priority",
      label: "Priority",
      value: record.priority,
      type: "select",
      required: true,
      options: priorityOptions,
    },
    {
      name: "area",
      label: "Area",
      value: record.area ?? "",
      type: "select",
      clearable: true,
      options: areaOptions(areas, record.area),
    },
    {
      name: "due_on",
      label: "Due date",
      value: record.dueOn ?? "",
      type: "date",
      clearable: true,
      hint: "Leave empty for no due date.",
    },
  ];
  const description = (
    <>
      <span className={styles.badges}>
        <PriorityMark priority={record.priority} />
        <span className={styles.flag}>{STATUS_LABEL[record.status]}</span>
        {record.woke ? <span className={styles.flagWoke}>Woke up</span> : null}
        {record.overdue ? (
          <span className={styles.flagLate}>Overdue</span>
        ) : null}
        {record.blocked ? <span className={styles.flag}>Blocked</span> : null}
      </span>{" "}
      Updated <LocalTime iso={record.updatedAt} zone={shell.timeZone} />
    </>
  );
  return (
    <Frame
      shell={shell}
      active="item"
      title={record.title}
      description={description}
      action={false}
    >
      <div className={styles.columns}>
        <div>
          <section className={styles.section} aria-labelledby="details">
            <h2 id="details">Details</h2>
            <dl className={styles.facts}>
              <div>
                <dt>Area</dt>
                <dd>{areaLabel(labels, record.area) || "No area"}</dd>
              </div>
              <div>
                <dt>Due</dt>
                <dd className={record.overdue ? styles.late : undefined}>
                  {record.dueOn ? day(record.dueOn) : "No due date"}
                </dd>
              </div>
              <div>
                <dt>Project</dt>
                <dd className={styles.factWithAction}>
                  {record.project ? (
                    <a href={shell.href("project", { ref: record.project.ref })}>
                      {record.project.title}
                    </a>
                  ) : (
                    <span>No project</span>
                  )}
                  {shell.submit && projectResult.state === "ok" ? (
                    <ProjectPicker
                      submit={shell.submit}
                      itemRef={record.ref}
                      revision={record.revision}
                      subject={record.title}
                      held={record.projectRef}
                      projects={pickable(projects)}
                      reloadHref={here}
                    />
                  ) : null}
                </dd>
              </div>
            </dl>
            {record.body ? (
              <p className={styles.body}>{record.body}</p>
            ) : (
              <p className={styles.meta}>No notes yet.</p>
            )}
            <details className={styles.disclosure}>
              <summary>Edit details</summary>
              <Form
                shell={shell}
                operation="current.item.update"
                base={base}
                fields={editFields}
                button="Save changes"
                reloadHref={here}
              />
            </details>
          </section>
          <section className={styles.section} aria-labelledby="comments">
            <h2 id="comments">Comments</h2>
            {record.comments.length ? (
              <ol className={styles.comments}>
                {record.comments.map((c) => (
                  <li
                    key={c.id}
                    className={c.kind === "system" ? styles.systemNote : undefined}
                  >
                    <p className={styles.commentMeta}>
                      <strong>{author(c)}</strong> <LocalTime iso={c.createdAt} zone={shell.timeZone} />
                    </p>
                    <p className={styles.body}>{c.body}</p>
                  </li>
                ))}
              </ol>
            ) : (
              <p className={styles.meta}>No comments yet.</p>
            )}
            <Form
              shell={shell}
              operation="current.item.comment"
              base={{ ref: record.ref }}
              fields={[
                {
                  name: "body",
                  label: "Add a comment",
                  type: "textarea",
                  required: true,
                  maxLength: 20000,
                },
              ]}
              button="Add comment"
              reset
            />
          </section>
        </div>
        <aside>
          <section className={styles.section} aria-labelledby="status">
            <h2 id="status">Status</h2>
            <p>
              {record.woke && record.snoozedUntil ? (
                <>
                  Woke up <LocalTime iso={record.snoozedUntil} zone={shell.timeZone} />
                </>
              ) : record.status === "snoozed" && record.snoozedUntil ? (
                <>
                  Snoozed until <LocalTime iso={record.snoozedUntil} zone={shell.timeZone} />
                </>
              ) : record.status === "done" && record.closedAt ? (
                <>
                  Done <LocalTime iso={record.closedAt} zone={shell.timeZone} />
                </>
              ) : (
                STATUS_LABEL[record.status]
              )}
            </p>
            {shell.submit ? (
              <StatusActions
                zone={shell.timeZone}
                submit={shell.submit}
                itemRef={record.ref}
                revision={record.revision}
                status={record.status}
                subject={record.title}
                reloadHref={here}
              />
            ) : null}
            {record.blocked && record.status !== "done" ? (
              <p className={styles.meta}>
                Still blocked. You can complete it anyway.
              </p>
            ) : null}
          </section>
          <section className={styles.section} aria-labelledby="dependencies">
            <h2 id="dependencies">Dependencies</h2>
            <h3 className={styles.subheading}>Blocked by</h3>
            <NeighbourList
              shell={shell}
              items={record.blockers}
              empty="Nothing unfinished blocks this."
              remove={(other) => ({
                item_ref: record.ref,
                blocker_ref: other.ref,
              })}
            />
            {candidates.length ? (
              <Form
                shell={shell}
                operation="current.dependency.add"
                base={{ item_ref: record.ref }}
                fields={[
                  {
                    name: "blocker_ref",
                    label: "Add a blocker",
                    type: "select",
                    required: true,
                    value: "",
                    options: choices,
                  },
                ]}
                button="Add blocker"
                inline
              />
            ) : null}
            <h3 className={styles.subheading}>Blocking</h3>
            <NeighbourList
              shell={shell}
              items={record.blocking}
              empty="This blocks nothing unfinished."
              remove={(other) => ({
                item_ref: other.ref,
                blocker_ref: record.ref,
              })}
            />
            {candidates.length ? (
              <Form
                shell={shell}
                operation="current.dependency.add"
                base={{ blocker_ref: record.ref }}
                fields={[
                  {
                    name: "item_ref",
                    label: "This blocks",
                    type: "select",
                    required: true,
                    value: "",
                    options: choices,
                  },
                ]}
                button="Add blocked item"
                inline
              />
            ) : null}
            {candidateResult.state === "ok" &&
            page(candidateResult, itemSummary).nextOffset !== null ? (
              <p className={styles.meta}>
                Lists the 100 most urgent active items.
              </p>
            ) : null}
          </section>
          <section className={styles.section} aria-labelledby="origin">
            <h2 id="origin">Origin</h2>
            <dl className={styles.facts}>
              <div>
                <dt>Source</dt>
                <dd>
                  {PROVENANCE_LABEL[record.provenance] ?? record.provenance}
                </dd>
              </div>
              {record.sourceKind ? (
                <div>
                  <dt>Source reference</dt>
                  <dd>
                    {record.sourceKind}: {record.sourceRef}
                  </dd>
                </div>
              ) : null}
              {record.sourceKind || record.eventCount > 1 ? (
                <div>
                  <dt>Signals</dt>
                  <dd>{record.eventCount}</dd>
                </div>
              ) : null}
              <div>
                <dt>Created</dt>
                <dd>
                  <LocalTime iso={record.createdAt} zone={shell.timeZone} />
                </dd>
              </div>
            </dl>
          </section>
        </aside>
      </div>
    </Frame>
  );
}

// --- new item ---------------------------------------------------------------------

export async function newItem({ shell, query }: ScreenProps) {
  const [areaResult, projectResult] = await Promise.all([
    read(shell, "current.area.list"),
    read(shell, "current.project.list"),
  ]);
  const projects = page(projectResult, projectOf).items;
  const preset = projects.some((p) => p.ref === query.project)
    ? (query.project ?? "")
    : "";
  return (
    <Frame
      shell={shell}
      active="new"
      title="New item"
      description="Something to do, chase or wait on. Only the title is required."
      action={false}
    >
      {areaResult.state !== "ok" ? (
        <Failure outcome={areaResult} />
      ) : (
        <Form
          shell={shell}
          operation="current.item.create"
          button="Add item"
          reset
          createdHref={shell.href("item")}
          createdLabel="Open the new item"
          fields={[
            { name: "title", label: "Title", required: true, maxLength: 500 },
            {
              name: "body",
              label: "Notes",
              type: "textarea",
              maxLength: 20000,
            },
            {
              name: "priority",
              label: "Priority",
              type: "select",
              required: true,
              value: "normal",
              options: priorityOptions,
            },
            {
              name: "area",
              label: "Area",
              type: "select",
              options: areaOptions(page(areaResult, areaOf).items, null),
            },
            {
              name: "project_ref",
              label: "Project",
              type: "select",
              value: preset,
              options: projectOptions(projects, null),
            },
            { name: "due_on", label: "Due date", type: "date" },
          ]}
        />
      )}
    </Frame>
  );
}

// --- projects ---------------------------------------------------------------------

function countsText(p: Project): string {
  const c = p.counts;
  const parts = [
    c.open ? `${c.open} open` : "",
    c.waiting ? `${c.waiting} waiting` : "",
    c.snoozed ? `${c.snoozed} snoozed` : "",
    c.done ? `${c.done} done` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : "No items";
}
const PROJECT_STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "done", label: "Done" },
];

export async function projects({ shell, query }: ScreenProps) {
  const statusFilter = PROJECT_STATUS_OPTIONS.some(
    (o) => o.value === query.status,
  )
    ? (query.status ?? null)
    : null;
  const [result, areaResult] = await Promise.all([
    read(shell, "current.project.list", { status: statusFilter }),
    read(shell, "current.area.list", { include_retired: true }),
  ]);
  const areas = page(areaResult, areaOf).items;
  const labels = areaLabels(areas);
  const list = page(result, projectOf).items;
  return (
    <Frame
      shell={shell}
      active="projects"
      title="Projects"
      description="Group related items. Deleting a project keeps its items."
    >
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : (
        <>
          <form
            action={shell.href("projects")}
            method="get"
            className={styles.toolbar}
          >
            <div className={styles.field}>
              <label htmlFor="project-status">Status</label>
              <select
                id="project-status"
                name="status"
                defaultValue={statusFilter ?? ""}
              >
                <option value="">Any status</option>
                {PROJECT_STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div className={styles.toolbarActions}>
              <button className={styles.secondary}>Apply</button>
            </div>
          </form>
          {list.length ? (
            <ul className={styles.rows} aria-label="Projects">
              {list.map((p) => (
                <li className={styles.row} key={p.ref}>
                  <div className={styles.rowMain}>
                    <a
                      className={styles.rowTitle}
                      href={shell.href("project", { ref: p.ref })}
                    >
                      {p.title}
                    </a>
                    <p className={styles.rowMeta}>
                      <span className={styles.flag}>
                        {PROJECT_STATUS_OPTIONS.find((o) => o.value === p.status)
                          ?.label ?? p.status}
                      </span>
                      {p.area ? <span>{areaLabel(labels, p.area)}</span> : null}
                      {p.dueOn ? <span>Due {day(p.dueOn)}</span> : null}
                      <span>{countsText(p)}</span>
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className={styles.empty}>
              <h2>{statusFilter ? "No projects match" : "No projects yet"}</h2>
              <p>
                {statusFilter
                  ? "Try another status."
                  : "A project gathers the items that move one piece of work forward."}
              </p>
            </div>
          )}
          <section className={styles.section} aria-labelledby="new-project">
            <h2 id="new-project">New project</h2>
            <Form
              shell={shell}
              operation="current.project.create"
              button="Create project"
              reset
              createdHref={shell.href("project")}
              createdLabel="Open the project"
              fields={[
                { name: "title", label: "Title", required: true, maxLength: 500 },
                {
                  name: "description",
                  label: "Description",
                  type: "textarea",
                  maxLength: 20000,
                },
                {
                  name: "area",
                  label: "Area",
                  type: "select",
                  options: areaOptions(areas, null),
                },
                { name: "due_on", label: "Due date", type: "date" },
              ]}
            />
          </section>
        </>
      )}
    </Frame>
  );
}

export async function project({ shell, query }: ScreenProps) {
  if (!query.ref)
    return (
      <Frame shell={shell} active="projects" title="Project">
        <div className={styles.empty}>
          <h2>No project selected</h2>
          <p>
            <a href={shell.href("projects")}>All projects</a>
          </p>
        </div>
      </Frame>
    );
  const status = query.status === "all" || query.status === "done"
    ? query.status
    : "active";
  const offset = pageOffset(query.offset);
  const [result, areaResult, itemResult, projectResult] =
    await Promise.all([
      read(shell, "current.project.get", { ref: query.ref }),
      read(shell, "current.area.list", { include_retired: true }),
      read(shell, "current.item.list", {
        project_ref: query.ref,
        status,
        limit: PAGE,
        offset,
      }),
      shell.submit
        ? read(shell, "current.project.list")
        : Promise.resolve(null),
    ]);
  if (result.state !== "ok")
    return (
      <Frame shell={shell} active="projects" title="Project">
        <Failure outcome={result} />
        <p>
          <a href={shell.href("projects")}>All projects</a>
        </p>
      </Frame>
    );
  const record = projectOf(result.result);
  const areas = page(areaResult, areaOf).items;
  const labels = areaLabels(areas);
  const list = page(itemResult, itemSummary);
  const here = shell.href("project", { ref: record.ref });
  const allProjects = page(projectResult, projectOf).items;
  return (
    <Frame
      shell={shell}
      active="projects"
      title={record.title}
      description={
        <span className={styles.headerMeta}>
          <span className={styles.flag}>
            {PROJECT_STATUS_OPTIONS.find((o) => o.value === record.status)
              ?.label ?? record.status}
          </span>
          <span>{countsText(record)}</span>
          {record.area ? <span>{areaLabel(labels, record.area)}</span> : null}
          {record.dueOn ? <span>Due {day(record.dueOn)}</span> : null}
        </span>
      }
      action={false}
    >
      <div className={styles.columns}>
        <div>
          <section className={styles.section} aria-labelledby="project-items">
            <div className={styles.sectionHead}>
              <h2 id="project-items">Items</h2>
              {shell.submit ? (
                <a
                  className={styles.secondary}
                  href={shell.href("new", { project: record.ref })}
                >
                  Add an item
                </a>
              ) : null}
            </div>
            <nav className={styles.segmented} aria-label="Item status">
              {(
                [
                  ["active", "Active"],
                  ["done", "Done"],
                  ["all", "All"],
                ] as const
              ).map(([value, text]) => (
                <a
                  key={value}
                  href={shell.href("project", { ref: record.ref, status: value })}
                  aria-current={status === value ? "page" : undefined}
                >
                  {text}
                </a>
              ))}
            </nav>
            {itemResult.state !== "ok" ? (
              <Failure outcome={itemResult} />
            ) : list.items.length ? (
              <DeskRows
                shell={shell}
                items={list.items}
                labels={labels}
                label="Project items"
                pick={
                  projectResult?.state === "ok" ? pickable(allProjects) : undefined
                }
                reloadHref={here}
              />
            ) : (
              <p className={styles.meta}>
                {status === "active"
                  ? "Nothing active in this project."
                  : "No items here."}
              </p>
            )}
            <Pager
              shell={shell}
              route="project"
              query={{ ref: record.ref, status }}
              offset={offset}
              shown={list.items.length}
              nextOffset={list.nextOffset}
            />
          </section>
        </div>
        <aside>
          <section className={styles.section} aria-labelledby="about">
            <h2 id="about">About</h2>
            {record.description ? (
              <p className={styles.body}>{record.description}</p>
            ) : (
              <p className={styles.meta}>No description.</p>
            )}
            <details className={styles.disclosure}>
              <summary>Edit project</summary>
              <Form
                shell={shell}
                operation="current.project.update"
                base={{ ref: record.ref, revision: record.revision }}
                button="Save project"
                reloadHref={here}
                fields={[
                  {
                    name: "title",
                    label: "Title",
                    value: record.title,
                    required: true,
                    maxLength: 500,
                  },
                  {
                    name: "description",
                    label: "Description",
                    value: record.description,
                    type: "textarea",
                    maxLength: 20000,
                  },
                  {
                    name: "status",
                    label: "Status",
                    value: record.status,
                    type: "select",
                    required: true,
                    options: PROJECT_STATUS_OPTIONS,
                  },
                  {
                    name: "area",
                    label: "Area",
                    value: record.area ?? "",
                    type: "select",
                    clearable: true,
                    options: areaOptions(areas, record.area),
                  },
                  {
                    name: "due_on",
                    label: "Due date",
                    value: record.dueOn ?? "",
                    type: "date",
                    clearable: true,
                    hint: "Leave empty for no due date.",
                  },
                ]}
              />
            </details>
          </section>
        </aside>
      </div>
    </Frame>
  );
}

// --- areas ------------------------------------------------------------------------

export async function areas({ shell }: ScreenProps) {
  if (shell.role !== "owner")
    return (
      <Frame shell={shell} active="areas" title="Areas" action={false}>
        <div className={styles.empty}>
          <h2>Only workspace owners manage areas</h2>
          <p>Ask an owner to add, rename or retire an area.</p>
        </div>
      </Frame>
    );
  const [result, summaryResult] = await Promise.all([
    read(shell, "current.area.list", { include_retired: true }),
    read(shell, "current.summary.get"),
  ]);
  const list = page(result, areaOf).items;
  const active = new Map(
    summaryResult.state === "ok"
      ? summary(summaryResult.result).byArea.map((a) => [a.area, a.active])
      : [],
  );
  return (
    <Frame
      shell={shell}
      active="areas"
      title="Areas"
      description="Each item and project sits in one area. Retiring an area hides it from new choices; items keep it."
      action={false}
    >
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : (
        <>
          {list.length ? (
            <ul className={styles.rows} aria-label="Areas">
              {list.map((a) => (
                <li className={styles.row} key={a.key}>
                  <div className={styles.rowMain}>
                    <span className={styles.rowTitle}>
                      {a.label}
                      {a.retired ? (
                        <span className={styles.flag}>Retired</span>
                      ) : null}
                    </span>
                    <p className={styles.rowMeta}>
                      <span>Key {a.key}</span>
                      <span>
                        {active.get(a.key) ?? 0} active{" "}
                        {(active.get(a.key) ?? 0) === 1 ? "item" : "items"}
                      </span>
                    </p>
                  </div>
                  {shell.submit ? (
                    <div className={styles.areaControls}>
                      <ActionForm
                        submit={shell.submit}
                        operation="current.area.update"
                        base={{ key: a.key }}
                        fields={[
                          {
                            name: "label",
                            label: `Rename ${a.label}`,
                            value: a.label,
                            required: true,
                            maxLength: 80,
                            hideLabel: true,
                          },
                        ]}
                        button="Rename"
                        inline
                      />
                      <ActionButton
                        submit={shell.submit}
                        operation="current.area.update"
                        input={{ key: a.key, retired: !a.retired }}
                        label={a.retired ? "Restore" : "Retire"}
                        description={`${a.retired ? "Restore" : "Retire"} ${a.label}`}
                      />
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <div className={styles.empty}>
              <h2>No areas</h2>
              <p>Add the first area below.</p>
            </div>
          )}
          <section className={styles.section} aria-labelledby="new-area">
            <h2 id="new-area">Add an area</h2>
            <Form
              shell={shell}
              operation="current.area.create"
              button="Add area"
              reset
              fields={[
                { name: "label", label: "Name", required: true, maxLength: 80 },
                {
                  name: "key",
                  label: "Key",
                  maxLength: 40,
                  hint: "Lowercase letters, digits and underscores. Left empty, it is made from the name. It cannot change later.",
                },
              ]}
            />
          </section>
        </>
      )}
    </Frame>
  );
}
