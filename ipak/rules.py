"""HBAC/Sudo drafts and staged application against inspected ipalib commands."""

from __future__ import annotations

from dataclasses import dataclass, field

from .backend import BackendError
from .directory import WriteResult


@dataclass(frozen=True)
class MemberField:
    attribute: str
    label: str
    kind: str
    command: str
    parameter: str


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    attribute: str | None
    members: tuple[MemberField, ...]


USERS = Category("users", "Users", "usercategory", (
    MemberField("memberuser_group", "User groups", "group", "user", "group"),
    MemberField("memberuser_user", "Individual users", "user", "user", "user")))
HOSTS = Category("hosts", "Hosts", "hostcategory", (
    MemberField("memberhost_hostgroup", "Host groups", "hostgroup", "host", "hostgroup"),
    MemberField("memberhost_host", "Individual hosts", "host", "host", "host")))
SERVICES = Category("services", "Services", "servicecategory", (
    MemberField("memberservice_hbacsvcgroup", "Service groups", "hbacsvcgroup", "service", "hbacsvcgroup"),
    MemberField("memberservice_hbacsvc", "Services", "hbacsvc", "service", "hbacsvc")))
ALLOW = Category("commands", "Allow commands", "cmdcategory", (
    MemberField("memberallowcmd_sudocmdgroup", "Allow command groups", "sudocmdgroup", "allow_command", "sudocmdgroup"),
    MemberField("memberallowcmd_sudocmd", "Allow commands", "sudocmd", "allow_command", "sudocmd")))
DENY = Category("deny", "Deny commands", None, (
    MemberField("memberdenycmd_sudocmdgroup", "Deny command groups", "sudocmdgroup", "deny_command", "sudocmdgroup"),
    MemberField("memberdenycmd_sudocmd", "Deny commands", "sudocmd", "deny_command", "sudocmd")))
RUNAS_USERS = Category("runas_users", "RunAs users", "ipasudorunasusercategory", (
    MemberField("ipasudorunas_user", "RunAs users", "user", "runasuser", "user"),
    MemberField("ipasudorunas_group", "RunAs user groups", "group", "runasuser", "group")))
RUNAS_GROUPS = Category("runas_groups", "RunAs groups", "ipasudorunasgroupcategory", (
    MemberField("ipasudorunasgroup_group", "RunAs groups", "group", "runasgroup", "group"),))
CATEGORIES = {"hbacrule": (USERS, HOSTS, SERVICES), "sudorule": (USERS, HOSTS, ALLOW, DENY, RUNAS_USERS, RUNAS_GROUPS)}


@dataclass
class RuleState:
    description: str = ""
    enabled: bool = False
    order: str = ""
    modes: dict[str, str] = field(default_factory=dict)
    members: dict[str, set[str]] = field(default_factory=dict)
    options: set[str] = field(default_factory=set)


@dataclass
class RuleDraft:
    kind: str
    key: str = ""
    original: RuleState = field(default_factory=RuleState)
    desired: RuleState = field(default_factory=RuleState)
    is_new: bool = True

    @property
    def dirty(self):
        return self.is_new or self.original != self.desired


def empty_state(kind: str) -> RuleState:
    return RuleState(modes={category.key: "unconfigured" for category in CATEGORIES[kind]},
                     members={member.attribute: set() for category in CATEGORIES[kind] for member in category.members})


def from_record(item) -> RuleState:
    state = empty_state(item.kind)
    if item.enabled is None:
        raise BackendError("unsupported", "Server did not report the rule enabled state; editing is unavailable.")
    state.description, state.enabled = item.description, item.enabled
    state.order = item.text("sudoorder", "")
    state.options = set(item.attributes.get("ipasudoopt", ()))
    for category in CATEGORIES[item.kind]:
        for member in category.members:
            state.members[member.attribute] = set(item.attributes.get(member.attribute, ()))
        if category.attribute and item.attributes.get(category.attribute) == ("all",):
            state.modes[category.key] = "all"
        elif any(state.members[member.attribute] for member in category.members):
            state.modes[category.key] = "selected"
    return state


def validate(draft: RuleDraft) -> list[str]:
    errors = []
    if not draft.key.strip():
        errors.append("Rule name is required")
    if draft.desired.order:
        try:
            value = int(draft.desired.order)
            if value < 0:
                raise ValueError
        except ValueError:
            errors.append("Sudo order must be a non-negative integer")
    required = {"users", "hosts", "services" if draft.kind == "hbacrule" else "commands"}
    for category in CATEGORIES[draft.kind]:
        mode = draft.desired.modes[category.key]
        members = any(draft.desired.members[member.attribute] for member in category.members)
        if mode not in {"unconfigured", "selected", "all"} or mode == "all" and not category.attribute:
            errors.append(f"Invalid mode for {category.label}")
        if mode in {"all", "unconfigured"} and members:
            errors.append(f"{category.label}: clear explicit selections or choose Selected")
        if draft.desired.enabled and category.key in required and (mode == "unconfigured" or mode == "selected" and not members):
            errors.append(f"{category.label}: choose explicit members or ALL before enabling")
    return errors


class RuleService:
    def __init__(self, directory):
        self.directory, self.backend = directory, directory.backend

    async def read(self, kind: str, key: str) -> RuleState:
        return from_record(await self.directory.get(kind, key, refresh=True))

    async def capabilities(self, kind):
        mod = await self.backend.supported(f"{kind}_mod")
        members = {}
        for category in CATEGORIES[kind]:
            for member in category.members:
                members[member.attribute] = member.parameter in await self.backend.supported(f"{kind}_add_{member.command}")
        return mod, members

    async def apply(self, draft: RuleDraft) -> list[WriteResult]:
        errors = validate(draft)
        if errors:
            return [WriteResult(draft.key, False, "; ".join(errors))]
        kind, key, desired = draft.kind, draft.key, draft.desired
        results = []

        async def refresh():
            self.directory.invalidate()
            state = await self.read(kind, key)
            draft.original = state
            return state

        async def call(command, target, **options):
            await self.backend.execute(command, key, **options)
            results.append(WriteResult(target, True, "Applied"))

        try:
            if draft.is_new:
                # Never populate a newly enabled rule. An empty rule matches no members.
                await call(f"{kind}_add", "Create empty rule")
                draft.is_new = False
                await call(f"{kind}_disable", "Disable new rule")
                current = await refresh()
                if current.enabled:
                    raise BackendError("validation-error", "The new rule was not disabled. No members were added.")
            else:
                current = await self.read(kind, key)
                if current != draft.original:
                    return [WriteResult(key, False, "Rule changed on server. Refresh the baseline and review your retained draft again.")]
                if current.enabled and draft.dirty:
                    await call(f"{kind}_disable", "Disable rule while applying")
                    current = await refresh()
                    if current.enabled:
                        raise BackendError("validation-error", "Rule could not be disabled; changes stopped.")

            # Clear ALL before adding explicit members; remove members before setting ALL.
            for category in CATEGORIES[kind]:
                mode = desired.modes[category.key]
                if category.attribute and current.modes[category.key] == "all" and mode != "all":
                    await call(f"{kind}_mod", f"Clear {category.label} ALL", **{category.attribute: None})
                    current = await refresh()
                for member in category.members:
                    wanted = desired.members[member.attribute]
                    actual = current.members[member.attribute]
                    for adding, targets in ((False, actual - wanted), (True, wanted - actual)):
                        if not targets:
                            continue
                        command = f"{kind}_{'add' if adding else 'remove'}_{member.command}"
                        response = await self.backend.execute(command, key, **{member.parameter: sorted(targets), "all": True})
                        current = await refresh()
                        failed = []
                        for target in sorted(targets):
                            success = (target in current.members[member.attribute]) == adding
                            results.append(WriteResult(f"{member.label}: {target}", success,
                                                       "Added" if success and adding else "Removed" if success else str(response.get("failed") or "Server did not apply membership")))
                            if not success:
                                failed.append(target)
                        if failed:
                            raise BackendError("partial", "Membership partially applied. Failed items remain in the draft; rule stays disabled.")
                if category.attribute and mode == "all" and current.modes[category.key] != "all":
                    await call(f"{kind}_mod", f"{category.label}: ALL", **{category.attribute: "all"})
                    current = await refresh()

            changes = {}
            if desired.description != current.description:
                changes["description"] = desired.description or None
            if kind == "sudorule" and desired.order != current.order:
                changes["sudoorder"] = int(desired.order) if desired.order else None
            if changes:
                await call(f"{kind}_mod", "Rule properties", **changes)
                current = await refresh()
            if kind == "sudorule":
                for adding, options in ((False, current.options - desired.options), (True, desired.options - current.options)):
                    for option in sorted(options):
                        await call(f"sudorule_{'add' if adding else 'remove'}_option", f"Sudo option: {option}", ipasudoopt=option)
                        current = await refresh()

            # Empty Selected is represented by the server as Unconfigured.
            for category in CATEGORIES[kind]:
                if desired.modes[category.key] == "selected" and not any(desired.members[member.attribute] for member in category.members):
                    desired.modes[category.key] = "unconfigured"
            expected = RuleState(desired.description, False, desired.order, desired.modes, desired.members, desired.options)
            actual = RuleState(current.description, False, current.order, current.modes, current.members, current.options)
            if actual != expected:
                raise BackendError("verification", "Read-back differs from the draft. Rule remains disabled; inspect the server response and retry.")
            if desired.enabled != current.enabled:
                await call(f"{kind}_{'enable' if desired.enabled else 'disable'}", "Enable rule" if desired.enabled else "Disable rule")
                current = await refresh()
            if current != desired:
                raise BackendError("verification", "Final state verification failed. Refresh and review again.")
            results.append(WriteResult(key, True, "Rule saved and verified"))
        except BackendError as error:
            results.append(WriteResult(key, False, str(error)))
            if not draft.is_new:
                try:
                    await refresh()
                except BackendError:
                    results.append(WriteResult(key, False, "Read-back unavailable; refresh the baseline before retrying."))
        return results
