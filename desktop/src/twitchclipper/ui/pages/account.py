"""Account page: optional Twitch sign-in with the Device Code flow (public client, no secret)."""

from __future__ import annotations

import asyncio
import logging
import webbrowser

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ...core.context import AppContext
from ...engine.jobs import Job, JobRunner
from ...twitch import auth
from ..widgets.common import hbox, notice, page_header

log = logging.getLogger("twitchclipper.account")

SCOPE_INFO = [
    ("user:read:chat", "Read chat in channels you monitor (required for live monitoring)", True),
    ("clips:edit", "Create Twitch clips (Modes A and B)", False),
    ("channel:manage:clips", "Download clips from your own channel (Modes B and E)", False),
    ("editor:manage:clips", "Download clips from channels where you're an editor (Modes B and E)", False),
]


class AccountPage(QWidget):
    def __init__(self, ctx: AppContext, runner: JobRunner, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx, self.runner = ctx, runner
        self._code: auth.DeviceCode | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(page_header("Account", "Signing in is optional. Local analysis works without it."))
        root.addWidget(notice(
            "TwitchClipper signs in with Twitch's official Device Code flow. You need your own free Twitch "
            "application: create one at <a href='https://dev.twitch.tv/console/apps'>dev.twitch.tv/console/apps</a> "
            "(Client type: <b>Public</b>, OAuth Redirect URL: <code>http://localhost</code>) and paste its Client ID "
            "below. Tokens are kept in Windows Credential Manager, never in plain files."))

        box = QGroupBox("Twitch application")
        form = QFormLayout(box)
        self.client_id = QLineEdit(ctx.settings.twitch.client_id)
        self.client_id.setPlaceholderText("Client ID")
        save = QPushButton("Save")
        save.clicked.connect(self.save_client_id)
        form.addRow("Client ID", hbox(self.client_id, save, stretch_end=False))
        root.addWidget(box)

        scopes = QGroupBox("Permissions to request")
        sl = QVBoxLayout(scopes)
        self.scope_boxes: dict[str, QCheckBox] = {}
        for scope, text, required in SCOPE_INFO:
            cb = QCheckBox(f"{text}\n{scope}")      # scope on its own line keeps the page narrow
            cb.setChecked(required)
            cb.setEnabled(not required)
            self.scope_boxes[scope] = cb
            sl.addWidget(cb)
        root.addWidget(scopes)

        self.signin_btn = QPushButton("Sign in with Twitch")
        self.signin_btn.setObjectName("Primary")
        self.signin_btn.clicked.connect(self.start_signin)
        self.signout_btn = QPushButton("Sign out")
        self.signout_btn.clicked.connect(self.sign_out)
        root.addWidget(hbox(self.signin_btn, self.signout_btn))

        self.code_box = QGroupBox("Finish signing in")
        cbl = QVBoxLayout(self.code_box)
        self.code_lbl = QLabel("")
        self.code_lbl.setObjectName("CardValue")
        self.code_hint = QLabel("")
        self.code_hint.setWordWrap(True)
        copy = QPushButton("Copy code")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._code.user_code if self._code else ""))
        open_btn = QPushButton("Open activation page")
        open_btn.clicked.connect(lambda: self._code and webbrowser.open(self._code.verification_uri))
        cbl.addWidget(self.code_lbl)
        cbl.addWidget(self.code_hint)
        cbl.addWidget(hbox(copy, open_btn))
        self.code_box.hide()
        root.addWidget(self.code_box)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        root.addStretch(1)
        ctx.events.account_changed.connect(self.refresh)
        self.refresh()

    def selected_scopes(self) -> list[str]:
        return [s for s, cb in self.scope_boxes.items() if cb.isChecked()]

    def refresh(self) -> None:
        login = self.ctx.account_login
        self.signout_btn.setEnabled(bool(login))
        self.signin_btn.setEnabled(bool(self.client_id.text().strip()))
        if login:
            scopes = self.ctx.db.kv_get("account_scopes", [])
            self.status.setText(f"Signed in as <b>{login}</b>. Granted: {', '.join(scopes) or '-'}")
        elif not self.client_id.text().strip():
            self.status.setText("Enter your application's Client ID to enable sign-in.")
        else:
            self.status.setText("Not signed in.")

    def save_client_id(self) -> None:
        self.ctx.settings.twitch.client_id = self.client_id.text().strip()
        self.ctx.save_settings()
        self.refresh()

    def start_signin(self) -> None:
        client_id = self.client_id.text().strip()
        if not client_id:
            return
        if client_id != self.ctx.settings.twitch.client_id:
            self.save_client_id()
        scopes = self.selected_scopes()
        self.signin_btn.setEnabled(False)
        self.status.setText("Contacting Twitch…")

        def start():
            async def go():
                da = auth.DeviceAuth(client_id)
                try:
                    return await da.start(scopes)
                finally:
                    await da.aclose()
            return asyncio.run(go())

        job = Job(start, pass_progress=False)
        job.signals.finished.connect(lambda code: self._show_code(code, client_id, scopes))
        job.signals.failed.connect(self._failed)
        self.runner.submit(job)

    def _show_code(self, code: auth.DeviceCode, client_id: str, scopes: list[str]) -> None:
        self._code = code
        self.code_lbl.setText(code.user_code)
        self.code_hint.setText(f"Open {code.verification_uri}, sign in to Twitch and enter this code. "
                               f"Waiting up to {code.expires_in // 60} minutes…")
        self.code_box.show()
        self.status.setText("Waiting for you to approve on twitch.tv…")

        def poll():
            async def go():
                da = auth.DeviceAuth(client_id)
                try:
                    tok = await da.poll(code, scopes)
                    info = await da.validate(tok)
                    return tok, info
                finally:
                    await da.aclose()
            return asyncio.run(go())

        job = Job(poll, pass_progress=False)
        job.signals.finished.connect(lambda r: self._signed_in(client_id, *r))
        job.signals.failed.connect(self._failed)
        self.runner.submit(job)

    def _signed_in(self, client_id: str, tok: auth.Token, info: dict | None) -> None:
        auth.save_token(client_id, tok)
        login = (info or {}).get("login") or "unknown"
        self.ctx.account_login = login
        self.ctx.db.kv_set("account_login", login)
        self.ctx.db.kv_set("account_scopes", tok.scopes)
        self.code_box.hide()
        self.ctx.events.account_changed.emit()

    def _failed(self, msg: str) -> None:
        self.code_box.hide()
        self.status.setText("Sign-in failed: " + msg.splitlines()[0])
        log.warning("sign-in failed: %s", msg)
        self.refresh()
        self.signin_btn.setEnabled(bool(self.client_id.text().strip()))

    def sign_out(self) -> None:
        cid = self.ctx.settings.twitch.client_id
        if cid:
            try:
                auth.delete_token(cid)
            except Exception as exc:  # noqa: BLE001
                log.warning("couldn't delete token: %s", exc)
        self.ctx.account_login = None
        self.ctx.db.kv_set("account_login", None)
        self.ctx.db.kv_set("account_scopes", [])
        self.ctx.events.account_changed.emit()
