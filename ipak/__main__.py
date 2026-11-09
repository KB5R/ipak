"""Run IPAK with deterministic demo data or the local system ipalib."""

import argparse

from .app import IPAKApp
from .backend import FakeBackend


def main() -> None:
    parser = argparse.ArgumentParser(description="IPAK — Textual administration for local FreeIPA")
    parser.add_argument("--live", action="store_true", help="use local system ipalib and the current Kerberos ticket")
    parser.add_argument("--scenario", choices=FakeBackend.SCENARIOS, default="normal",
                        help="fake response scenario for manual checks")
    args = parser.parse_args()
    if args.live:
        from .ipa import IPABackend
        backend = IPABackend()
    else:
        backend = FakeBackend(args.scenario)
    IPAKApp(backend).run()


if __name__ == "__main__":
    main()
