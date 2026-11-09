"""CSV drafts, deterministic login generation, preflight and resumable creation."""

from __future__ import annotations

import csv
import io
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from uuid import uuid4

from .backend import BackendError
from .directory import WriteResult


USER_COLUMNS = (("ФИО*", "full_name"), ("Почта", "mail"), ("Телефон", "telephonenumber"),
                ("Отдел", "ou"), ("Должность", "title"), ("Логин", "login"), ("Комментарий", "carlicense"))
HOST_COLUMNS = (("FQDN*", "fqdn"), ("IP", "ip_address"), ("Описание", "description"))
TRANSLITERATION = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                          ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya")))


@dataclass(frozen=True)
class LoginPolicy:
    separator: str = "."

    def generate(self, given: str, surname: str) -> str:
        def convert(part):
            text = "".join(TRANSLITERATION.get(char, char) for char in part.strip().lower())
            text = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
            text = re.sub(r"\s+", "-", text)
            if not text or not re.fullmatch(r"[a-z][a-z0-9-]*", text):
                raise ValueError("Cannot transliterate this name; enter a login explicitly.")
            return text
        return convert(given) + self.separator + convert(surname)


@dataclass
class DraftRow:
    values: dict[str, str]
    line: int = 0
    id: str = field(default_factory=lambda: uuid4().hex)
    groups: set[str] = field(default_factory=set)
    groups_done: set[str] = field(default_factory=set)
    created: bool = False
    generated_login: bool = False
    errors: list[str] = field(default_factory=list)
    status: str = "Draft"

    def key(self, kind):
        return self.values.get("login" if kind == "user" else "fqdn", "").strip()


def template(kind: str, order: str = "surname_given") -> str:
    if kind == "user":
        example = "Иванов Иван Иванович" if order == "surname_given" else "Иван Иванов"
        return ("# * = обязательное поле; пустой Логин будет сгенерирован после проверки имени\n"
                "# Разделитель: ;  •  Строки с # — комментарии\n"
                + ";".join(label for label, _ in USER_COLUMNS) + "\n"
                + f"# {example};ivanov@example.test;;ИТ;Инженер;;Пример\n")
    return "# * = обязательное поле; строки с # — комментарии\nFQDN*;IP;Описание\n# host.example.test;192.0.2.10;Пример\n"


def normalize_name(row: DraftRow, order: str, policy: LoginPolicy) -> None:
    parts = row.values.get("full_name", "").split()
    if order == "surname_given" and len(parts) in {2, 3}:
        surname, given = parts[:2]
    elif order == "given_surname" and len(parts) == 2:
        given, surname = parts
    else:
        row.values["givenname"] = row.values["sn"] = ""
        return
    row.values["givenname"], row.values["sn"] = given, surname
    if not row.values.get("login") or row.generated_login:
        try:
            row.values["login"] = policy.generate(given, surname)
            row.generated_login = True
        except ValueError:
            row.values["login"] = ""


def parse_csv(content: str, kind: str, order: str = "surname_given", policy: LoginPolicy = LoginPolicy()) -> list[DraftRow]:
    columns = USER_COLUMNS if kind == "user" else HOST_COLUMNS
    names = {label.rstrip("*").casefold(): name for label, name in columns}
    reader = csv.reader(io.StringIO(content.lstrip("\ufeff")), delimiter=";", strict=True)
    header = None
    rows = []
    try:
        for cells in reader:
            if not cells or all(not cell.strip() for cell in cells) or cells[0].lstrip().startswith("#"):
                continue
            if header is None:
                labels = [cell.strip().rstrip("*").casefold() for cell in cells]
                unknown = [label for label in labels if label not in names]
                if unknown or len(set(labels)) != len(labels):
                    raise ValueError("Unknown or duplicate CSV columns: " + ", ".join(unknown or labels))
                header = [names[label] for label in labels]
                required = "full_name" if kind == "user" else "fqdn"
                if required not in header:
                    raise ValueError("Missing required CSV header: " + columns[0][0])
                continue
            values = {name: value.strip() for name, value in zip(header, cells)}
            row = DraftRow(values, line=reader.line_num)
            if len(cells) != len(header):
                row.errors.append(f"Line {reader.line_num}: expected {len(header)} cells; received {len(cells)}")
            if kind == "user":
                normalize_name(row, order, policy)
            else:
                row.values["fqdn"] = row.values.get("fqdn", "").lower().rstrip(".")
            rows.append(row)
    except csv.Error as error:
        raise ValueError(f"CSV line {reader.line_num}: {error}") from None
    if header is None:
        raise ValueError("CSV header is missing")
    return rows


def serialize_csv(rows: list[DraftRow], kind: str) -> str:
    stream = io.StringIO()
    stream.write("# Current TUI rows; groups and applied status stay in this session.\n")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    columns = USER_COLUMNS if kind == "user" else HOST_COLUMNS
    writer.writerow([label for label, _ in columns])
    for row in rows:
        writer.writerow([row.values.get(name, "") for _, name in columns])
    return stream.getvalue()


def merge_editor_rows(previous: list[DraftRow], incoming: list[DraftRow], kind: str) -> list[DraftRow]:
    by_key = {row.key(kind): row for row in previous if row.key(kind)}
    incoming_keys = {row.key(kind) for row in incoming}
    for old in previous:
        if (old.groups or old.created) and old.key(kind) not in incoming_keys:
            raise ValueError(f"Keep {old.key(kind)} in CSV to preserve its groups/applied status; change or remove it in the table with undo.")
    for row in incoming:
        old = by_key.get(row.key(kind))
        if old:
            if old.created:
                # Normalize names are not serialized; compare the editable CSV fields only.
                columns = USER_COLUMNS if kind == "user" else HOST_COLUMNS
                if any(row.values.get(key, "") != old.values.get(key, "") for _, key in columns):
                    raise ValueError(f"{old.key(kind)} was already created; its applied fields cannot be rewritten.")
            row.id = old.id
            row.groups = set(old.groups)
            row.groups_done = set(old.groups_done)
            row.created = old.created
            row.status = old.status
            if row.values.get("full_name") == old.values.get("full_name"):
                for name in ("givenname", "sn"):
                    if name in old.values:
                        row.values[name] = old.values[name]
    return incoming


def validate_rows(rows: list[DraftRow], kind: str, supported: frozenset[str] | None = None) -> bool:
    counts = {}
    for row in rows:
        key = row.key(kind).casefold()
        counts[key] = counts.get(key, 0) + 1
    for row in rows:
        row.errors = [error for error in row.errors if error.startswith("Line ")]
        key = row.key(kind)
        if not key:
            row.errors.append("Login is required" if kind == "user" else "FQDN is required")
        elif counts[key.casefold()] > 1:
            row.errors.append("Duplicate target in this draft")
        if kind == "user":
            if not row.values.get("full_name"):
                row.errors.append("Full name is required")
            if not row.values.get("givenname") or not row.values.get("sn"):
                row.errors.append("Confirm Given name and Surname; the full name is ambiguous")
            if key and not re.fullmatch(r"[a-z_][a-z0-9_.-]*\$?", key):
                row.errors.append("Invalid login; use lowercase Latin letters, digits, dot, underscore or hyphen")
            if row.values.get("mail") and any("@" not in address for address in row.values["mail"].split(",")):
                row.errors.append("Invalid email address")
            optional = ("mail", "telephonenumber", "ou", "title", "carlicense")
            if supported is not None:
                row.errors.extend(f"Server does not support {name}" for name in optional if row.values.get(name) and name not in supported)
        else:
            if key and ("." not in key or any(not label or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in key.split("."))):
                row.errors.append("Invalid FQDN")
            if row.values.get("ip_address"):
                try:
                    ipaddress.ip_address(row.values["ip_address"])
                except ValueError:
                    row.errors.append("Invalid IP address")
    return bool(rows) and not any(row.errors for row in rows)


class BulkCreator:
    def __init__(self, service, kind: str):
        self.service, self.kind = service, kind

    async def preflight(self, rows: list[DraftRow], progress=None) -> bool:
        supported = await self.service.supported(f"{self.kind}_add")
        if self.kind == "user" and "random" not in supported:
            raise BackendError("unsupported", "Server cannot generate temporary passwords for user creation.")
        valid = validate_rows(rows, self.kind, supported)
        known_groups = {}
        for index, row in enumerate(rows):
            if progress:
                progress(index + 1, len(rows), row)
            if row.errors:
                continue
            try:
                if not row.created and await self.service.exists(self.kind, row.key(self.kind)):
                    row.errors.append("Already exists on server; correct the target. No automatic suffix is added.")
                for group in sorted(row.groups - row.groups_done):
                    if group not in known_groups:
                        known_groups[group] = await self.service.exists("group" if self.kind == "user" else "hostgroup", group)
                    if not known_groups[group]:
                        row.errors.append(f"Group not found: {group}")
            except BackendError as error:
                row.errors.append(str(error))
        return valid and not any(row.errors for row in rows)

    async def apply(self, rows: list[DraftRow], progress=None) -> list[WriteResult]:
        results = []
        for index, row in enumerate(rows):
            key = row.key(self.kind)
            if progress:
                progress(index + 1, len(rows), row)
            if row.errors:
                results.append(WriteResult(key, False, "; ".join(row.errors)))
                continue
            if not row.created:
                if self.kind == "user":
                    options = {name: row.values[name] for name in ("givenname", "sn", "mail", "telephonenumber", "ou", "title", "carlicense") if row.values.get(name)}
                    options["cn"] = row.values["full_name"]
                    if "mail" in options:
                        options["mail"] = [address.strip() for address in options["mail"].split(",")]
                else:
                    options = {name: row.values[name] for name in ("ip_address", "description") if row.values.get(name)}
                result = await self.service.create(self.kind, key, options)
                results.append(result)
                if not result.success:
                    row.status = result.message
                    continue
                row.created = True
                row.status = "Created"
            for group in sorted(row.groups - row.groups_done):
                result = await self.service.change_member(self.kind, key, group, True)
                results.append(WriteResult(f"{key} → {group}", result.success, result.message, verified=result.verified))
                if result.success:
                    row.groups_done.add(group)
                else:
                    row.status = "Created; membership failed"
            if row.groups.issubset(row.groups_done):
                row.status = "Complete"
        return results
