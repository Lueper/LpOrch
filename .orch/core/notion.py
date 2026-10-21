"""A Notion database as the task list: reading its task rows, writing status and results back, and adding a row
for every follow-up task an agent asks for.

Talks to the Notion REST API over urllib, so nothing has to be installed and `preview` stays SDK-free.
orch.toml only says which database it is ([tasks] source = "notion", [notion] database and token_env); the
property names and status options are fixed below, so the database has to use exactly these names.
core/tasksource.py writes the rows into TASKS.md as its cache, and the run reads and writes that file as usual.

One row is one task, laid out like a TASKS.md line and its detail lines:
    task     title      the task ID "T01" - what "after: T01" and "ref: T01" point at
    title    rich text  the task title
    kind     select     "doc" | "code" | "sys" | "test" | "task", or "code:refactorer" for a role skill
    status   status     pending | running | done | failed - the [ ] [~] [x] [!] markers
    ref      rich text  base materials, one per line (path, glob, URL or task ID); each one is a "ref:" line
    details  rich text  the other detail lines, one per line ("after: T01", "lang: en", "verify: ...", instructions)
    result   rich text  written by the orchestrator when a task succeeds: "doc/x.md (run 20261008-101500)"
Rows are taken in task ID order, and a follow-up row gets the next free ID, so existing IDs never move.

The integration must be invited to the database (Share -> Connections) and its token read from an
environment variable (token_env, default NOTION_TOKEN); orch.toml is committed, so it holds no secret.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from core.config import DEFAULT_TOKEN_ENV, NotionSettings
from core.taskfile import DIRECTIVE_PATTERN, KINDS, RESULT_ARROW
from core.tasks import (
    STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING, TASK_KINDS, FollowUp, Task, TaskFileError, TaskId,
)

API_ROOT = "https://api.notion.com/v1"
# Pinned: from 2025-09-03 on, queries move to data sources and this module's endpoints change with them
API_VERSION = "2022-06-28"
REQUEST_TIMEOUT = 20  # seconds per API request
RETRY_STATUSES = {429, 500, 502, 503, 504}  # rate limits and Notion-side hiccups are worth another attempt
MAX_ATTEMPTS = 3
RETRY_WAIT = 2.0  # seconds, multiplied by the attempt number
PAGE_SIZE = 100  # rows per query request (the API's maximum)
MAX_TEXT_CHARS = 2000  # the API's limit per text chunk; titles and detail blocks written here stay well below
MAX_ROWS = 500  # stop reading there instead of pulling an unexpectedly huge database into every prompt
ID_PATTERN = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)  # a Notion id once its dashes are removed
# The database's properties, name -> Notion property type. Fixed in code rather than in orch.toml:
# the database must use exactly these names (Notion names are case-sensitive)
PROP_TASK = "task"
PROP_TITLE = "title"
PROP_TYPE = "kind"
PROP_STATUS = "status"
PROP_REF = "ref"
PROP_DETAILS = "details"
PROP_RESULT = "result"
PROPERTY_TYPES = {
    PROP_TASK: "title", PROP_TITLE: "rich_text", PROP_TYPE: "select", PROP_STATUS: "status",
    PROP_REF: "rich_text", PROP_DETAILS: "rich_text", PROP_RESULT: "rich_text",
}
# The Status option of each TASKS.md marker; the API cannot add status options, so the property must have these
STATUS_OPTIONS = {STATUS_PENDING: "pending", STATUS_RUNNING: "running", STATUS_DONE: "done", STATUS_FAILED: "failed"}
MARKERS = {option: marker for marker, option in STATUS_OPTIONS.items()}
TASK_ID_VALUE = re.compile(r"^T\s*(\d+)$", re.IGNORECASE)  # a Task cell: "T01", "t1"
KIND_VALUE = re.compile(rf"^(?P<kind>{KINDS})(?::\s*(?P<role>[\w-]+))?$", re.IGNORECASE)  # a kind option
REF_PREFIX = re.compile(r"^ref\s*:\s*", re.IGNORECASE)  # a Ref line may carry the "ref:" of a TASKS.md line
# Oldest row first: of two rows with the same task ID, the older one keeps it
ROW_ORDER = [{"timestamp": "created_time", "direction": "ascending"}]


class NotionError(TaskFileError):
    """The database cannot be read or a row can no longer be written; handled like a TASKS.md failure."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status, so a caller can add a hint for "not found" or "no access"


def DatabaseId(value: str) -> str | None:
    """The database id in a copied Notion link or a bare id, without dashes; None when there is none.

    The "?v=..." view id is dropped first, so the path's own id is the one that is taken.
    """
    path = value.strip().split("?")[0].split("#")[0]
    found = ID_PATTERN.findall(path.replace("-", ""))
    return found[-1].lower() if found else None  # Notion puts the id at the end of the path, after the title slug


def TokenEnvName(settings: NotionSettings) -> str:
    return settings.tokenEnv or DEFAULT_TOKEN_ENV


def ReadToken(settings: NotionSettings) -> str | None:
    return (os.environ.get(TokenEnvName(settings)) or "").strip() or None


# ---------- API ----------

def Request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    """One Notion API call; retries rate limits and transient server errors, then gives up with a NotionError."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": API_VERSION,
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{API_ROOT}{path}", data=data, headers=headers, method=method)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                time.sleep(RetryWait(error, attempt))
                continue
            raise NotionError(f"Notion API {method} {path}: {error.code} {ErrorMessage(error)}", error.code) from error
        except (urllib.error.URLError, OSError, ValueError) as error:  # network down, timeout, broken JSON
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_WAIT * attempt)
                continue
            raise NotionError(f"Notion API {method} {path}: {error}") from error
    raise NotionError(f"Notion API {method} {path}: no response after {MAX_ATTEMPTS} attempts")


def RetryWait(error: urllib.error.HTTPError, attempt: int) -> float:
    header = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(float(header), 30.0) if header else RETRY_WAIT * attempt
    except ValueError:
        return RETRY_WAIT * attempt


def ErrorMessage(error: urllib.error.HTTPError) -> str:
    """Notion's own message for a failed call, so the user sees what to fix (sharing, id, property type)."""
    try:
        body = json.loads(error.read().decode("utf-8", errors="replace"))
    except (ValueError, OSError):
        return error.reason or ""
    return str(body.get("message") or body.get("code") or error.reason or "")


def RetrieveDatabase(token: str, databaseId: str) -> dict:
    return Request("GET", f"/databases/{databaseId}", token)


def QueryRows(token: str, databaseId: str, sorts: list[dict]) -> list[dict]:
    """Every row of the database in the given order, following the API's pagination."""
    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": PAGE_SIZE, "sorts": sorts}
        if cursor:
            payload["start_cursor"] = cursor
        data = Request("POST", f"/databases/{databaseId}/query", token, payload)
        rows += data.get("results", [])
        cursor = data.get("next_cursor") if data.get("has_more") else None
        if not cursor or len(rows) >= MAX_ROWS:
            return rows[:MAX_ROWS]


def UpdateRow(token: str, pageId: str, properties: dict) -> None:
    Request("PATCH", f"/pages/{pageId}", token, {"properties": properties})


def CreateRow(token: str, databaseId: str, properties: dict) -> dict:
    """Add a row to the database; the created page, whose "url" is where the new task now lives."""
    return Request("POST", "/pages", token, {"parent": {"database_id": databaseId}, "properties": properties})


# ---------- property values ----------

def PlainText(value: dict | None) -> str:
    """The text of a title or rich_text property, newlines and all."""
    if not value:
        return ""
    pieces = value.get(value.get("type", ""), [])
    if not isinstance(pieces, list):
        return ""
    return "".join(piece.get("plain_text", "") for piece in pieces if isinstance(piece, dict))


def OptionText(value: dict | None) -> str:
    """The chosen option of a status or select property, or the text of a title or rich_text property."""
    if not value:
        return ""
    kind = value.get("type", "")
    if kind in ("status", "select"):
        chosen = value.get(kind) or {}
        return str(chosen.get("name") or "")
    return PlainText(value)


def TextValue(text: str) -> dict:
    """A rich_text property value; one chunk is enough for the short strings written here."""
    return {"rich_text": [{"type": "text", "text": {"content": text[:MAX_TEXT_CHARS]}}] if text else []}


def TitleValue(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:MAX_TEXT_CHARS]}}] if text else []}


def OneLine(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def TextLines(text: str) -> list[str]:
    """The non-empty lines of a multi-line text property, trimmed."""
    return [line.strip() for line in text.splitlines() if line.strip()]


# ---------- schema ----------

def SchemaProblems(database: dict) -> list[str]:
    """What the database lacks of the fixed layout above; any of it stops the run before a row is read."""
    properties = database.get("properties") or {}
    problems = []
    for name, kind in PROPERTY_TYPES.items():
        found = (properties.get(name) or {}).get("type")
        if found != kind:
            problems.append(f"'{name}' is {found}, not {kind}" if found else f"no '{name}' property ({kind})")
    if (properties.get(PROP_STATUS) or {}).get("type") == "status":
        options = (properties[PROP_STATUS].get("status") or {}).get("options") or []
        names = {option.get("name") for option in options}
        missing = [name for name in STATUS_OPTIONS.values() if name not in names]
        if missing:
            problems.append(f"'{PROP_STATUS}' has no option {', '.join(missing)}")
    return problems


# ---------- rows -> tasks ----------

@dataclass
class Row:
    """One task row as read: what tasksource.py writes to TASKS.md and then reads back as a Task."""

    pageId: str
    number: int  # the task ID's number: T07 -> 7
    kind: str
    role: str | None
    title: str
    status: str  # a TASKS.md marker
    details: list[str]  # detail lines as TASKS.md holds them: Details first, then each Ref line as "ref: ..."
    result: str  # "doc/x.md (run 20261008-101500)"; empty until the task first succeeds

    @property
    def taskId(self) -> str:
        return TaskId(self.number)

    def Lines(self) -> list[str]:
        """The task as TASKS.md lines without line endings, in the form ParseTasks reads back."""
        role = f":{self.role}" if self.role else ""
        lines = [f"- [{self.status}] [{self.kind}{role}] {self.title}", *(f"  {line}" for line in self.details)]
        return lines + ([f"  {RESULT_ARROW} result: {self.result}"] if self.result else [])


def Cell(page: dict, name: str) -> str:
    return OptionText((page.get("properties") or {}).get(name)).strip()


def TaskNumber(page: dict) -> int | None:
    match = TASK_ID_VALUE.match(Cell(page, PROP_TASK))
    return int(match[1]) if match else None


def ReadRow(page: dict) -> tuple[Row | None, str | None]:
    """One row as a Row, or why it is not read as a task (reported like an unreadable TASKS.md line)."""
    cells = {name: Cell(page, name) for name in PROPERTY_TYPES}
    if not any(cells[name] for name in PROPERTY_TYPES if name != PROP_STATUS):
        return None, None  # the blank row a Notion database usually ends with (Status always has a default)
    number, title = TaskNumber(page), OneLine(cells[PROP_TITLE])
    kind = KIND_VALUE.match(cells[PROP_TYPE])
    status = MARKERS.get(cells[PROP_STATUS]) if cells[PROP_STATUS] else STATUS_PENDING
    problem = None
    if number is None:
        found = f", not '{cells[PROP_TASK]}'" if cells[PROP_TASK] else ""
        problem = f"{PROP_TASK} must hold a task ID like T01{found}"
    elif not title:
        problem = f"{PROP_TITLE} is empty"
    elif not kind:
        problem = f"{PROP_TYPE} must be one of {', '.join(TASK_KINDS)}, not '{cells[PROP_TYPE]}'"
    elif status is None:
        problem = f"{PROP_STATUS} '{cells[PROP_STATUS]}' is not one of {', '.join(STATUS_OPTIONS.values())}"
    if problem:
        return None, f"notion row {cells[PROP_TASK] or '(no ID)'} '{title}': {problem}; not read as a task"
    refs = [f"ref: {REF_PREFIX.sub('', line)}" for line in TextLines(cells[PROP_REF])]
    return Row(
        pageId=str(page.get("id") or ""), number=number, kind=kind["kind"].lower(), role=kind["role"], title=title,
        status=status, details=TextLines(cells[PROP_DETAILS]) + refs, result=OneLine(cells[PROP_RESULT]),
    ), None


# ---------- the database as a task list ----------

@dataclass
class NotionDatabase:
    """One database, read once per run: its rows as tasks, and the writes back to those rows."""

    settings: NotionSettings
    databaseId: str
    token: str
    warnings: list[str] = field(default_factory=list)
    lastNumber: int = 0  # the highest task ID among the rows; a follow-up row takes the next one
    # Property name -> its id, read by Check(). Writes are keyed by id because the API resolves a key by name or id:
    # the title property's id is always "title", so the rich_text property named "title" cannot be written by name
    propertyIds: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"Notion database {self.databaseId}"

    def Check(self) -> None:
        """Stop with what to fix when the database cannot be read or lacks the fixed properties."""
        try:
            database = RetrieveDatabase(self.token, self.databaseId)
        except NotionError as error:
            if error.status not in (401, 403, 404):  # a real API or network failure says enough on its own
                raise
            raise NotionError(
                f"{error}; check the token in {TokenEnvName(self.settings)}, that the integration is invited to the"
                " database (Share -> Connections), and that the link points to a database rather than a page that"
                " holds an inline database",
                error.status,
            ) from error
        if database.get("object") != "database":
            raise NotionError(
                f"{self.databaseId} is a Notion {database.get('object', 'object')}, not a database; open the"
                " database as a full page and copy that link ([notion] database in orch.toml)"
            )
        problems = SchemaProblems(database)
        if problems:
            raise NotionError(
                f"{self.label} does not have the task properties LpOrch expects: {'; '.join(problems)}"
                " (the names are fixed in .orch/core/notion.py; the [notion] comment in orch.toml lists them)"
            )
        properties = database.get("properties") or {}
        self.propertyIds = {name: str(properties[name].get("id") or name) for name in PROPERTY_TYPES}

    def ById(self, values: dict[str, dict]) -> dict[str, dict]:
        """Property values keyed by property id instead of name, as every write sends them (see propertyIds)."""
        return {self.propertyIds.get(name, name): value for name, value in values.items()}

    def Rows(self) -> list[Row]:
        """The task rows in task ID order; a row that cannot be read as a task becomes a warning instead."""
        self.Check()
        pages = QueryRows(self.token, self.databaseId, ROW_ORDER)
        self.warnings = []
        rows: dict[int, Row] = {}
        for page in pages:
            row, warning = ReadRow(page)
            if row and row.number in rows:  # ROW_ORDER reads the older row first, and it keeps the ID
                warning = (f"notion row {row.taskId} '{row.title}': {row.taskId} is already the ID of"
                           f" '{rows[row.number].title}'; not read as a task")
            elif row:
                rows[row.number] = row
            if warning:
                self.warnings.append(warning)
        # Rows not read as a task still hold their ID, so a follow-up row never reuses it
        self.lastNumber = max((number for number in map(TaskNumber, pages) if number is not None), default=0)
        if len(pages) >= MAX_ROWS:
            self.warnings.append(f"notion: only the first {MAX_ROWS} rows of the database are read")
        return [rows[number] for number in sorted(rows)]

    def WriteStatus(self, task: Task, status: str) -> None:
        if task.sourceId:
            UpdateRow(self.token, task.sourceId, self.ById({PROP_STATUS: {"status": {"name": STATUS_OPTIONS[status]}}}))

    def WriteResult(self, task: Task) -> None:
        if not task.sourceId:
            return
        run = f" (run {task.resultRun})" if task.resultRun else ""  # later tasks find its reviews in that run
        value = re.sub(r"[\r\n]+", " ", task.resultRef) + run
        UpdateRow(self.token, task.sourceId, self.ById({PROP_RESULT: TextValue(value)}))

    def AddTask(self, followUp: FollowUp) -> tuple[str, str]:
        """Create a pending row for a new task under the next free task ID; returns that ID and where the row is.

        The row is filled the way ReadRow reads it back: "ref:" detail lines go to Ref, the others to Details.
        """
        refs, details = [], []
        for line in followUp.details:
            directive = DIRECTIVE_PATTERN.match(line)
            if directive and directive["key"].lower() == "ref":
                refs += [directive["value"].strip()] if directive["value"].strip() else []
            else:
                details.append(line)
        number = self.lastNumber + 1
        role = f":{followUp.role}" if followUp.role else ""
        properties = {
            PROP_TASK: TitleValue(TaskId(number)),
            PROP_TITLE: TextValue(followUp.title),
            PROP_TYPE: {"select": {"name": f"{followUp.kind}{role}"}},  # Notion adds a missing select option itself
            PROP_STATUS: {"status": {"name": STATUS_OPTIONS[STATUS_PENDING]}},
            PROP_REF: TextValue("\n".join(refs)),
            PROP_DETAILS: TextValue("\n".join(details)),
        }
        page = CreateRow(self.token, self.databaseId, self.ById(properties))
        self.lastNumber = number  # only once the row exists, so a failed call does not skip an ID
        return TaskId(number), f"Notion {TaskId(number)} {page.get('url') or page.get('id') or ''}".rstrip()


def OpenDatabase(settings: NotionSettings) -> tuple[NotionDatabase | None, list[str]]:
    """The configured database, or the settings errors that stop the run before any agent starts."""
    databaseId = DatabaseId(settings.database or "")
    token = ReadToken(settings)
    errors = []
    if not settings.database:
        errors.append('[tasks] source = "notion" needs [notion] database in orch.toml (the database link or its id)')
    elif not databaseId:
        errors.append(f"[notion] database '{settings.database}' holds no Notion id (expected 32 hex characters)")
    if not token:
        errors.append(
            f"[tasks] source = \"notion\" needs the integration token in the {TokenEnvName(settings)} environment"
            " variable, and the integration invited to the database (Share -> Connections)"
        )
    if errors:
        return None, errors
    return NotionDatabase(settings=settings, databaseId=databaseId, token=token), []
