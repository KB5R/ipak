"""In-memory API surface for the demo and write-flow tests; no network calls."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace

from .backend import BackendError
from .directory import KEYS
from .ipa import user_detail


class FakeAPI:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.calls = []
        self.failures: dict[tuple[str, str], str] = {}
        self.user_attributes = {}
        self.records = {
            "host": {"web.example.test": {"fqdn": ("web.example.test",), "description": ("Web server",),
                                          "memberof_hostgroup": ("webservers",)}},
            "group": {name: {"cn": (name,), "description": (f"{name} user group",),
                              "member_user": tuple(user for user, detail in backend.details.items() if name in detail.direct_groups)}
                      for name in ("admins", "developers", "ipausers", "operators")},
            "hostgroup": {"webservers": {"cn": ("webservers",), "description": ("Web servers",),
                                         "member_host": ("web.example.test",)}},
            "hbacrule": {"ssh_access": {"cn": ("ssh_access",), "description": ("SSH access",),
                                        "ipaenabledflag": (True,), "memberuser_group": ("developers",),
                                        "memberhost_hostgroup": ("webservers",), "memberservice_hbacsvc": ("sshd",)}},
            "sudorule": {"operations": {"cn": ("operations",), "description": ("Operations",),
                                        "ipaenabledflag": (False,), "memberuser_group": ("operators",)}},
            "hbacsvc": {"sshd": {"cn": ("sshd",), "description": ("Demo SSH service",)}},
            "hbacsvcgroup": {"remote": {"cn": ("remote",), "member_hbacsvc": ("sshd",)}},
            "sudocmd": {"/usr/bin/id": {"sudocmd": ("/usr/bin/id",)}},
            "sudocmdgroup": {"diagnostics": {"cn": ("diagnostics",), "member_sudocmd": ("/usr/bin/id",)}},
        }

    def users(self):
        rows = {}
        for key, detail in self.backend.details.items():
            row = {
                "uid": (key,), "cn": (detail.user.full_name,), "givenname": (detail.given_name or detail.user.full_name.split()[0],),
                "sn": (detail.surname or " ".join(detail.user.full_name.split()[1:]),),
                "nsaccountlock": not detail.user.enabled, "mail": detail.emails,
                "carlicense": (detail.comment,), "memberof_group": detail.direct_groups,
                "memberofindirect_group": detail.indirect_groups,
            }
            row = {**self.user_attributes.get(key, {}), **row}
            if detail.password_expires:
                row["krbpasswordexpiration"] = (detail.password_expires,)
                row["krblastpwdchange"] = (detail.password_expires,)
            for field, value in (("memberof_hbacrule", detail.associated_hbac), ("memberof_sudorule", detail.associated_sudo)):
                if value is not None:
                    row[field] = value
            rows[key] = row
        return rows

    def sync_user(self, key, row):
        self.user_attributes[key] = {name: value for name, value in row.items() if name not in {"userpassword", "randompassword"}}
        detail = user_detail(row)
        self.backend.details[key] = detail
        self.backend.users = tuple(item for item in self.backend.users if item.login != key) + (detail.user,)

    async def supported(self, command):
        relation = command.split("_", 2)[-1]
        if relation in {"user", "host", "service", "allow_command", "deny_command", "runasuser", "runasgroup"}:
            return frozenset({"user", "group", "host", "hostgroup", "hbacsvc", "hbacsvcgroup", "sudocmd", "sudocmdgroup", "all"})
        return frozenset({"givenname", "sn", "cn", "mail", "carlicense", "telephonenumber", "ou", "title", "random",
                          "description", "ip_address", "usercategory", "hostcategory", "servicecategory", "cmdcategory",
                          "ipasudorunasusercategory", "ipasudorunasgroupcategory", "sudoorder", "setattr"})

    async def execute(self, command, *args, **options):
        # Only names are retained; passwords are never placed in the spy.
        self.calls.append((command, args, tuple(sorted(options))))
        await asyncio.sleep(self.backend.delay)
        kind, action = command.split("_", 1)
        key = args[0] if args else None
        if (command, key) in self.failures:
            raise BackendError("validation-error", self.failures[(command, key)])
        rows = self.users() if kind == "user" else self.records.setdefault(kind, {})
        if action == "find":
            query = (key or "").casefold()
            result = [dict(row) for name, row in rows.items() if query in f"{name} {row.get('description', '')} {row.get('cn', '')}".casefold()]
            limit = options.get("sizelimit", 48)
            return {"result": tuple(result[:limit]), "truncated": len(result) > limit}
        if action == "show":
            if key not in rows:
                raise BackendError("not-found")
            return {"result": dict(rows[key])}
        if action == "add":
            if key in rows:
                raise BackendError("validation-error", f"{key} already exists")
            row = {KEYS.get(kind, "cn"): (key,)}
            if kind == "user":
                row.update({"cn": (options.get("cn") or f"{options['givenname']} {options['sn']}",),
                            "nsaccountlock": False, "memberof_group": ("ipausers",)})
            if kind in {"hbacrule", "sudorule"}:
                row["ipaenabledflag"] = (True,)
            rows[key] = row
        elif key not in rows:
            raise BackendError("not-found")
        else:
            row = rows[key]
        if action in {"add", "mod"}:
            for field, value in options.items():
                if field not in {"random", "all", "raw", "setattr"}:
                    row[field] = tuple(value) if isinstance(value, (tuple, list)) else (str(value),) if value is not None else ()
            secret = ""
            if options.get("random"):
                secret = secrets.token_urlsafe(20)
                row["krbpasswordexpiration"] = (self.backend.reference_time,)
                row["krblastpwdchange"] = (self.backend.reference_time,)
            if kind == "user":
                self.sync_user(key, row)
            result = dict(row)
            if secret:
                result["randompassword"] = secret
            return {"result": result}
        if action in {"enable", "disable"}:
            if kind == "user":
                row["nsaccountlock"] = action == "disable"
                self.sync_user(key, row)
            else:
                row["ipaenabledflag"] = (action == "enable",)
            return {"result": True}
        if action == "del":
            del rows[key]
            if kind == "user":
                del self.backend.details[key]
                self.backend.users = tuple(user for user in self.backend.users if user.login != key)
            return {"result": {"failed": ()}}
        if action in {"add_member", "remove_member"}:
            member_kind = "user" if kind == "group" else "host"
            members = set(row.get(f"member_{member_kind}", ()))
            targets = options.get(member_kind, ())
            completed = 0
            failed = []
            member_rows = self.users() if member_kind == "user" else self.records["host"]
            for target in targets:
                if target not in member_rows:
                    failed.append((target, "Not found"))
                    continue
                adding = action.startswith("add")
                members.add(target) if adding else members.discard(target)
                member_row = member_rows[target]
                groups = set(member_row.get(f"memberof_{kind}", ()))
                groups.add(key) if adding else groups.discard(key)
                member_row[f"memberof_{kind}"] = tuple(sorted(groups))
                if member_kind == "user":
                    self.sync_user(target, member_row)
                completed += 1
            row[f"member_{member_kind}"] = tuple(sorted(members))
            return {"result": dict(row), "completed": completed, "failed": {"member": {member_kind: tuple(failed)}}}
        if kind in {"hbacrule", "sudorule"} and action.startswith(("add_", "remove_")):
            adding, relation = action.split("_", 1)
            prefixes = {"user": "memberuser", "host": "memberhost", "service": "memberservice",
                        "allow_command": "memberallowcmd", "deny_command": "memberdenycmd",
                        "runasuser": "ipasudorunas", "runasgroup": "ipasudorunasgroup"}
            for member_kind, members in options.items():
                if member_kind in {"all", "raw", "no_members", "version"}:
                    continue
                field = "ipasudoopt" if relation == "option" else f"{prefixes[relation]}_{member_kind}"
                members = (members,) if isinstance(members, str) else members
                current = set(row.get(field, ()))
                current.update(members) if adding == "add" else current.difference_update(members)
                row[field] = tuple(sorted(current))
            return {"result": dict(row), "completed": 1, "failed": {}}
        raise BackendError("unsupported", f"Demo does not support {command}")
