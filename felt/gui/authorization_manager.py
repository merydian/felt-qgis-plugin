"""
Felt Authorization Manager
"""

import platform
from functools import partial
from typing import Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    QObject,
    pyqtSignal,
    QTimer,
    QDate
)
from qgis.PyQt.QtNetwork import (
    QNetworkReply,
    QNetworkRequest
)
from qgis.PyQt.QtWidgets import (
    QAction,
    QPushButton
)
from qgis.core import (
    Qgis,
    QgsSettings,
    QgsApplication
)
from qgis.gui import (
    QgsMessageBarItem
)
from qgis.utils import iface

from .authorize_dialog import AuthorizeDialog
from ..core import (
    AuthState,
    OAuthWorkflow,
    API_CLIENT,
    User
)

AUTH_CONFIG_ID = "felt_auth_id"
AUTH_CONFIG_EXPIRY = "felt_auth_expiry"


class AuthorizationManager(QObject):
    """
    Handles the GUI component of client authorization
    """

    authorized = pyqtSignal()
    authorization_failed = pyqtSignal()
    status_changed = pyqtSignal(AuthState)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.status: AuthState = AuthState.NotAuthorized
        self._workflow: Optional[OAuthWorkflow] = None
        self.oauth_close_timer: Optional[QTimer] = None

        self._authorizing_message = None
        self._authorization_failed_message = None

        self.queued_callbacks = []
        self.user: Optional[User] = None
        self._user_reply: Optional[QNetworkReply] = None

        self.login_action = QAction(self.tr('Sign In…'))
        self.login_action.triggered.connect(self._login_action_triggered)

    @staticmethod
    def remove_api_token():
        """
        Remove stored API token
        """
        if platform.system() == "Darwin":
            # remove stored plain text tokens on MacOS
            QgsSettings().remove("felt/token", QgsSettings.Plugins)
        else:
            QgsApplication.authManager().removeAuthSetting(AUTH_CONFIG_ID)

    @staticmethod
    def store_api_token(token: str, expiry: int) -> bool:
        """
        Stores the API token in the secure QGIS password store, IF available

        Returns True if the key could be stored
        """
        expiry_day = QDate.currentDate().addDays(int(expiry / 60 / 60 / 24))
        if platform.system() == "Darwin":
            # store tokens in plain text on MacOS as keychain isn't
            # available due to MacOS security
            QgsSettings().setValue(
                "felt/token", token, QgsSettings.Plugins
            )
            QgsSettings().setValue(
                "felt/token_expiry",
                expiry_day.toString('yyyy-MM-dd'),
                QgsSettings.Plugins
            )
        else:
            QgsApplication.authManager().storeAuthSetting(
                AUTH_CONFIG_ID, token, True
            )
            QgsApplication.authManager().storeAuthSetting(
                AUTH_CONFIG_EXPIRY,
                expiry_day.toString('yyyy-MM-dd'),
                True
            )
        return True

    @staticmethod
    def retrieve_api_token() -> Optional[str]:
        """
        Retrieves a previously stored API token, if available

        Returns None if no stored token is available
        """
        if platform.system() == "Darwin":
            api_token = QgsSettings().value(
                "felt/token", None, str, QgsSettings.Plugins
            )
            token_expiry = QgsSettings().value(
                "felt/token_expiry", None, str, QgsSettings.Plugins
            )
            if not api_token:
                api_token = None
        else:
            api_token = (
                    QgsApplication.authManager().authSetting(
                        AUTH_CONFIG_ID, defaultValue="", decrypt=True
                    ) or None
            )
            token_expiry = (
                    QgsApplication.authManager().authSetting(
                        AUTH_CONFIG_EXPIRY, defaultValue="", decrypt=True
                    ) or None
            )

        if token_expiry:
            token_expiry_date = QDate.fromString(token_expiry, 'yyyy-MM-dd')
            if token_expiry_date <= QDate.currentDate():
                api_token = None
        else:
            api_token = None

        return api_token

    def _set_status(self, status: AuthState):
        """
        Sets the current authorization status
        """
        if self.status == status:
            return

        self.status = status
        self.status_changed.emit(self.status)

        if self.status == AuthState.NotAuthorized:
            self.login_action.setText(self.tr('Sign In…'))
            self.login_action.setEnabled(True)
            self.user = None
        elif self.status == AuthState.Authorizing:
            self.login_action.setText(self.tr('Authorizing…'))
            self.login_action.setEnabled(False)
            self.user = None
        elif self.status == AuthState.Authorized:
            self.login_action.setText(self.tr('Log Out'))
            self.login_action.setEnabled(True)

    def is_authorized(self) -> bool:
        """
        Returns True if the client is authorized
        """
        return self.status == AuthState.Authorized

    def _login_action_triggered(self):
        """
        Called when the login action is triggered
        """
        if self.status == AuthState.NotAuthorized:
            self.attempt_authorize()
        elif self.status == AuthState.Authorized:
            self.deauthorize()

    def authorization_callback(self, callback) -> bool:
        """
        Returns True if the client is already authorized, or False
        if an authorization is in progress and the operation needs to wait
        for the authorized signal before proceeding
        """
        if self.status == AuthState.Authorized:
            callback()
            return True

        self.queued_callbacks.append(callback)
        self.attempt_authorize()
        return False

    def deauthorize(self):
        """
        Deauthorizes the client
        """
        self._set_status(AuthState.NotAuthorized)
        API_CLIENT.set_token(None)
        AuthorizationManager.remove_api_token()

    def attempt_authorize(self):
        """
        Tries to authorize the client, using previously fetched token if
        available. Otherwise shows the login dialog to the user.
        """
        previous_token = AuthorizationManager.retrieve_api_token()
        if previous_token:
            self._cleanup_messages()

            self._set_status(AuthState.Authorized)
            API_CLIENT.set_token(previous_token)
            self.authorized.emit()
            self._user_reply = API_CLIENT.user()
            self._user_reply.finished.connect(partial(self._set_user_details,
                                                      self._user_reply))
            return

        self.show_authorization_dialog()

    def show_authorization_dialog(self):
        """
        Shows the authorization dialog before commencing the authorization
        process
        """
        dlg = AuthorizeDialog()
        if dlg.exec_():
            self.start_authorization_workflow()
        else:
            self.queued_callbacks = []

    def start_authorization_workflow(self):
        """
        Start an authorization process
        """
        assert not self._workflow

        self._cleanup_messages()

        self._workflow = OAuthWorkflow()
        self._workflow.error_occurred.connect(
            self._authorization_error_occurred
        )
        self._workflow.finished.connect(self._authorization_success)

        self._set_status(AuthState.Authorizing)

        self._authorizing_message = QgsMessageBarItem(self.tr('Felt'),
                                                      self.tr('Authorizing…'),
                                                      Qgis.MessageLevel.Info)
        iface.messageBar().pushItem(self._authorizing_message)

        self._workflow.start()
        return False

    def _cleanup_messages(self):
        """
        Removes outdated message bar items
        """
        if self._authorizing_message and not sip.isdeleted(
                self._authorizing_message):
            iface.messageBar().popWidget(self._authorizing_message)
            self._authorizing_message = None
        if self._authorization_failed_message and not sip.isdeleted(
                self._authorization_failed_message):
            iface.messageBar().popWidget(self._authorization_failed_message)
            self._authorization_failed_message = None

    def _authorization_error_occurred(self, error: str):
        """
        Triggered when an authorization error occurs
        """
        self.queued_callbacks = []
        self._cleanup_messages()

        self._clean_workflow()

        self._set_status(AuthState.NotAuthorized)
        login_error = self.tr('Authorization error - {}'.format(error))

        self._authorization_failed_message = QgsMessageBarItem(
            self.tr('Felt'),
            login_error,
            Qgis.MessageLevel.Critical
        )

        retry_button = QPushButton(self.tr("Try Again"))
        retry_button.clicked.connect(self.show_authorization_dialog)
        self._authorization_failed_message.layout().addWidget(retry_button)

        iface.messageBar().pushItem(self._authorization_failed_message)

        self.queued_callbacks = []
        self.authorization_failed.emit()

    def _authorization_success(self, token: str, expiry: int):
        """
        Triggered when an authorization succeeds
        """
        self._cleanup_messages()

        self._set_status(AuthState.Authorized)
        iface.messageBar().pushSuccess(self.tr('Felt'), self.tr('Authorized'))
        API_CLIENT.set_token(token)
        AuthorizationManager.store_api_token(token, expiry)

        self._clean_workflow()

        self.authorized.emit()

        self._user_reply = API_CLIENT.user()
        self._user_reply.finished.connect(partial(self._set_user_details,
                                                  self._user_reply))

    def _set_user_details(self, reply: QNetworkReply):
        """
        Sets user details
        """
        if reply != self._user_reply:
            # an outdated reply we don't care about anymore
            return

        if not self._user_reply or sip.isdeleted(self._user_reply):
            self._user_reply = None
            return

        if self._user_reply.attribute(
                QNetworkRequest.HttpStatusCodeAttribute) == 401:
            self._user_reply = None
            self.deauthorize()
            self.attempt_authorize()
            return

        if self._user_reply.error() != QNetworkReply.NoError:
            self._user_reply = None
            return

        self.user = User.from_json(reply.readAll().data().decode())
        callbacks = self.queued_callbacks
        self.queued_callbacks = []
        for callback in callbacks:
            callback()

    def cleanup(self):
        """
        Must be called when the authorization handler needs to be gracefully
        shutdown (e.g. on plugin unload)
        """
        if self._user_reply and not sip.isdeleted(self._user_reply):
            self._user_reply.deleteLater()
        self._user_reply = None

        self._close_auth_server(force_close=True)

    def _clean_workflow(self):
        """
        Cleans up the oauth workflow
        """
        if self._workflow and not sip.isdeleted(self._workflow):
            self.oauth_close_timer = QTimer(self)
            self.oauth_close_timer.setSingleShot(True)
            self.oauth_close_timer.setInterval(1000)
            self.oauth_close_timer.timeout.connect(self._close_auth_server)
            self.oauth_close_timer.start()

    def _close_auth_server(self, force_close=False):
        """
        Gracefully closes and cleans up the oauth workflow
        """
        if self.oauth_close_timer and not sip.isdeleted(
                self.oauth_close_timer):
            self.oauth_close_timer.timeout.disconnect(
                self._close_auth_server)
            self.oauth_close_timer.deleteLater()
        self.oauth_close_timer = None

        if self._workflow and not sip.isdeleted(self._workflow):
            if force_close:
                self._workflow.force_stop()

            self._workflow.close_server()
            self._workflow.quit()
            self._workflow.wait()
            self._workflow.deleteLater()

        self._workflow = None


AUTHORIZATION_MANAGER = AuthorizationManager()
