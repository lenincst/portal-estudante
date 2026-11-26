"""Command line: `python -m portal_estudante setup | login | status | check | notifications`.

setup  — store YOUR login (RA) once. The password is NOT stored by default: you type it on the official sign-in page.
         `--save-password` (opt-in) keeps it in the operating-system keychain so later logins can run unattended;
         `--password-stdin` reads it from standard input (for scripts) instead of prompting.
login  — sign in (opens the official page for you to type the password, unless you saved it) and keep the session.
status — show what is configured (never values).
check  — run every read operation once (verifies your access and detects portal changes). Read-only.
notifications — listen for new notifications (Ctrl+C to stop).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import config
from .secrets import LocalStore, SecretStore, load_config, save_config


def _setup(args: argparse.Namespace) -> int:
    print("portal-estudante — cliente não oficial. Suas credenciais ficam só neste computador e só são enviadas ao login oficial.")
    user = (args.user or input("Seu login (RA ou RA@ulife.com.br): ")).strip()
    if not user:
        print("Login vazio; nada foi salvo.")
        return 1
    if "@" not in user:
        user = f"{user}@ulife.com.br"
    save_config({"backend": "local"})
    store = LocalStore()
    store.put("ULIFE_USERNAME", user)
    if args.save_password:
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Senha (não aparece na tela; fica no cofre do sistema): ")
        if not password:
            print("Senha vazia; não foi salva.")
            return 1
        store.put("ULIFE_PASSWORD", password)
        print("Senha guardada no cofre do sistema operacional.")
    else:
        print("A senha NÃO foi guardada: você vai digitá-la na página oficial de login.")
    print("Próximo passo: python -m portal_estudante login")
    return 0


def _status(_: argparse.Namespace) -> int:
    cfg = load_config()
    print("configurado:", "sim" if cfg else "não (rode: python -m portal_estudante setup)")
    if cfg:
        keys = SecretStore().keys()
        print("login salvo:", "ULIFE_USERNAME" in keys, "| senha salva:", "ULIFE_PASSWORD" in keys, "| sessão:", "ULIFE_SESSION" in keys)
    print("dados locais:", config.DATA_DIR)
    return 0


def _login(_: argparse.Namespace) -> int:
    from .client import PortalClient

    with PortalClient() as c:
        c.http.relogin()
        print(f"Login OK. Cursos encontrados: {len(c.courses())}")
    return 0


def _check(args: argparse.Namespace) -> int:
    from .client import PortalClient
    from .errors import PortalError

    failures = 0
    with PortalClient(classmates=args.classmates) as c:
        for name, thunk in c.read_checks():
            try:
                data = thunk()
                print(f"OK    {name:<34} {len(data) if isinstance(data, (list, dict)) else 1:>4} itens", flush=True)
            except PortalError as exc:
                failures += 1
                print(f"FALHA {name:<34} {type(exc).__name__}: {str(exc)[:300]}", flush=True)
    print("falhas:", failures)
    return 1 if failures else 0


def _notifications(_: argparse.Namespace) -> int:
    from .client import PortalClient
    from .notifications import NotificationWatcher, _print

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    watcher = NotificationWatcher(PortalClient(), _print)
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portal_estudante")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup", help="guardar seu login (a senha só se você pedir)")
    p.add_argument("--user", help="seu login (RA); se omitido, é perguntado")
    p.add_argument("--save-password", action="store_true", help="guardar a senha no cofre do sistema (opcional)")
    p.add_argument("--password-stdin", action="store_true", help="com --save-password: ler a senha da entrada padrão")
    p.set_defaults(fn=_setup)
    sub.add_parser("login", help="entrar e guardar a sessão").set_defaults(fn=_login)
    sub.add_parser("status", help="ver configuração (sem valores)").set_defaults(fn=_status)
    p = sub.add_parser("check", help="testar todas as leituras (somente leitura)")
    p.add_argument("--classmates", action="store_true", help="incluir a lista da turma (dados de colegas; opt-in)")
    p.set_defaults(fn=_check)
    sub.add_parser("notifications", help="escutar novas notificações").set_defaults(fn=_notifications)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
