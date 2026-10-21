"""Where the task list lives: TASKS.md or a Notion database, behind one interface. SDK-free.

[tasks] source in orch.toml (or --source) picks the backend:
    file    <project>/TASKS.md, parsed by core/taskfile.py (the default)
    notion  a Notion database read and written through core/notion.py, cached in TASKS.md: every Read() rewrites
            the file's task list from the rows, and the run then works on the file as with "file"
Both answer the same questions the run needs: the tasks, the lines or rows that could not be read, and the
writes back - a task's status marker, its "result:" location, and a new pending task for each follow-up an
agent asked for.
"""
from __future__ import annotations

from pathlib import Path

from core.config import SOURCE_NOTION, OrchConfig
from core.notion import NotionDatabase, OpenDatabase
from core.paths import TASKS_FILE
from core.taskfile import AppendTask, ParseTasks, RecordResult, ReplaceTaskList, UnreadTaskLines, UpdateTaskStatus
from core.tasks import FollowUp, Task, TaskFileError


class TaskSource:
    """The task list of one run; Warnings() is asked after Read(), which is what fills it for a database."""

    label = "task list"

    def Read(self) -> list[Task]:
        raise NotImplementedError

    def Warnings(self) -> list[str]:
        return []

    def Notes(self) -> list[str]:
        """Lines preview prints under the source, e.g. that TASKS.md was rewritten from Notion."""
        return []

    def SetStatus(self, task: Task, status: str) -> None:
        raise NotImplementedError

    def RecordResult(self, task: Task) -> None:
        raise NotImplementedError

    def AddTask(self, followUp: FollowUp) -> tuple[str, str]:
        """Add a new pending task at the end of the list; returns its task ID and where it went."""
        raise NotImplementedError

    def NextNumber(self) -> int:
        """The number the next added task gets (5 = T05); asked after Read()."""
        raise NotImplementedError


class FileTaskSource(TaskSource):
    """TASKS.md: tasks are lines, and every write edits the file in place."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.label = str(path)

    def Read(self) -> list[Task]:
        return ParseTasks(self.path)

    def Warnings(self) -> list[str]:
        return UnreadTaskLines(self.path)

    def SetStatus(self, task: Task, status: str) -> None:
        UpdateTaskStatus(self.path, task, status)

    def RecordResult(self, task: Task) -> None:
        RecordResult(self.path, task)

    def AddTask(self, followUp: FollowUp) -> tuple[str, str]:
        where = AppendTask(self.path, followUp)
        return ParseTasks(self.path)[-1].id, where  # IDs follow line order, and the new task is the last line

    def NextNumber(self) -> int:
        return len(ParseTasks(self.path)) + 1


class NotionTaskSource(TaskSource):
    """A Notion database cached in TASKS.md: tasks are rows, and the run works on their copy in the file.

    Read() rewrites the file's task list from the rows (the header stays) and parses it back, so the run sees
    exactly what a TASKS.md with these lines gives - except that the task IDs come from the Task property and
    every task remembers its row. Each write goes to the row first - the database is the task list, the file only
    its copy that the next Read() rebuilds - and then to the file, so TASKS.md follows the run as well.

    A Notion write is one synchronous API call, made only when a task's status or result changes or a follow-up
    task is added. A failing call gives up after MAX_ATTEMPTS x REQUEST_TIMEOUT (core/notion.py), so the
    orchestrator keeps the follow-up writes off the event loop (orchestrate.RunPipeline).
    """

    def __init__(self, database: NotionDatabase, cache: Path) -> None:
        self.database = database
        self.cache = FileTaskSource(cache)
        self.label = f"{database.label} (cached in {cache.name})"

    def Read(self) -> list[Task]:
        rows = self.database.Rows()
        ReplaceTaskList(self.cache.path, [line for row in rows for line in row.Lines()])
        tasks = self.cache.Read()
        if [(task.kind, task.title) for task in tasks] != [(row.kind, row.title) for row in rows]:
            raise TaskFileError(f"{self.cache.path} does not read back as the rows of {self.database.label}")
        for task, row in zip(tasks, rows):  # the file numbers its tasks by line; the Task property is the real ID
            task.id, task.sourceId = row.taskId, row.pageId
        return tasks

    def Warnings(self) -> list[str]:
        return list(self.database.warnings)

    def Notes(self) -> list[str]:
        return [f"{self.cache.path.name} is the cache: its task list was rewritten from the database rows"]

    def SetStatus(self, task: Task, status: str) -> None:
        self.database.WriteStatus(task, status)
        try:
            self.cache.SetStatus(task, status)
        finally:
            task.status = status  # even when the file's line is gone: the row already says so

    def RecordResult(self, task: Task) -> None:
        self.database.WriteResult(task)
        self.cache.RecordResult(task)

    def AddTask(self, followUp: FollowUp) -> tuple[str, str]:
        taskId, where = self.database.AddTask(followUp)  # the row's ID; the file would number it by line
        try:
            return taskId, f"{where}; {self.cache.AddTask(followUp)[1]}"
        except (TaskFileError, OSError) as error:  # the row exists, and the next Read() copies it to the file
            return taskId, f"{where}; not copied to {self.cache.path.name}: {error}"

    def NextNumber(self) -> int:
        return self.database.lastNumber + 1


def CreateTaskSource(config: OrchConfig, projectDir: Path) -> tuple[TaskSource | None, list[str]]:
    """The configured task source, or the settings errors that keep the run from starting."""
    tasksFile = projectDir / TASKS_FILE.name
    if config.taskSource == SOURCE_NOTION:
        database, errors = OpenDatabase(config.notion)
        return (NotionTaskSource(database, tasksFile) if database else None), errors
    return FileTaskSource(tasksFile), []
