# -*- coding: utf-8 -*-
"""
/***************************************************************************
 CloudProjectsDialog
                                 A QGIS plugin
 Sync your projects to QField on android
                             -------------------
        begin                : 2020-07-28
        git sha              : $Format:%H$
        copyright            : (C) 2020 by OPENGIS.ch
        email                : info@opengis.ch
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.core import Qgis, QgsApplication, QgsProject
from qgis.PyQt.QtCore import (
    QDateTime,
    QItemSelectionModel,
    QRegularExpression,
    Qt,
    QUrl,
    pyqtSignal,
)
from qgis.PyQt.QtGui import (
    QFont,
    QIcon,
    QPixmap,
    QRegularExpressionValidator,
    QValidator,
)
from qgis.PyQt.QtNetwork import QNetworkReply
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QToolButton,
    QTreeWidgetItem,
    QWidget,
)
from qgis.PyQt.uic import loadUiType
from qgis.utils import iface

from qfieldsync.core import Preferences
from qfieldsync.core.cloud_api import (
    CloudException,
    CloudNetworkAccessManager,
    from_reply,
)
from qfieldsync.core.cloud_project import CloudProject, ProjectFile, ProjectFileCheckout
from qfieldsync.gui.cloud_converter_dialog import CloudConverterDialog
from qfieldsync.gui.cloud_login_dialog import CloudLoginDialog
from qfieldsync.gui.cloud_transfer_dialog import CloudTransferDialog
from qfieldsync.utils.cloud_utils import closure, to_cloud_title
from qfieldsync.utils.permissions import can_change_project_owner, can_delete_project
from qfieldsync.utils.qgis_utils import get_qgis_files_within_dir
from qfieldsync.utils.qt_utils import rounded_pixmap

CloudProjectsDialogUi, _ = loadUiType(
    str(Path(__file__).parent.joinpath("../ui/cloud_projects_dialog.ui"))
)


class LocalDirFeedback(Enum):
    Error = "error"
    Warning = "warning"
    Success = "success"


class CloudProjectsDialog(QDialog, CloudProjectsDialogUi):
    projects_refreshed = pyqtSignal()
    _current_cloud_project = None

    def __init__(
        self,
        network_manager: CloudNetworkAccessManager,
        parent: QWidget = None,
        project: CloudProject = None,
    ) -> None:
        """Constructor."""
        super(CloudProjectsDialog, self).__init__(parent=parent)
        self.setupUi(self)
        self.setWindowModality(Qt.WindowModal)
        self.preferences = Preferences()
        self.network_manager = network_manager
        self._current_cloud_project = project
        self.transfer_dialog = None
        self.project_transfer = None

        self.update_welcome_label()

        if not self.network_manager.has_token():
            CloudLoginDialog.show_auth_dialog(
                self.network_manager,
                lambda: self.on_auth_accepted(),
                lambda: self.close(),
                parent=self,
            )
        else:
            self.show_projects()

        self.use_current_project_directory_action = QAction(
            QIcon(), self.tr("Use Current Project Directory")
        )
        self.use_current_project_directory_action.triggered.connect(
            self.on_use_current_project_directory_action_triggered
        )

        self.projectNameLineEdit.setValidator(
            QRegularExpressionValidator(
                QRegularExpression("^[a-zA-Z][-a-zA-Z0-9_]{2,}$")
            )
        )

        # TODO show when public projects are ready
        self.projectsType.hide()
        self.projectsType.addItem(self.tr("My projects"))
        self.projectsType.addItem(self.tr("Community"))
        self.projectsType.setCurrentIndex(0)
        self.projectsType.currentIndexChanged.connect(lambda: self.show_projects())

        self.projectsTable.setColumnWidth(0, int(self.projectsTable.width() / 2))
        self.projectsTable.setColumnWidth(1, int(self.projectsTable.width() / 3.25))
        self.projectsTable.setColumnWidth(2, int(self.projectsTable.width() / 10))

        self.synchronizeButton.clicked.connect(
            lambda: self.on_project_sync_button_clicked()
        )
        self.synchronizeButton.setEnabled(False)
        self.editButton.setIcon(
            QgsApplication.getThemeIcon("/mActionProjectProperties.svg")
        )
        self.editButton.clicked.connect(lambda: self.on_project_edit_button_clicked())
        self.editButton.setEnabled(False)
        self.openButton.setIcon(QgsApplication.getThemeIcon("/mActionFileOpen.svg"))
        self.openButton.clicked.connect(lambda: self.on_project_launch_button_clicked())
        self.openButton.setEnabled(False)
        self.deleteButton.clicked.connect(
            lambda: self.on_project_delete_button_clicked()
        )
        self.deleteButton.setEnabled(False)

        self.projectsStack.setCurrentWidget(self.projectsListPage)
        self.createProjectWidget = CloudConverterDialog(
            iface,
            self.network_manager,
            QgsProject.instance(),
            self,
        )
        self.projectCreatePage.layout().addWidget(self.createProjectWidget)

        self.refreshButton.setIcon(QgsApplication.getThemeIcon("/mActionRefresh.svg"))
        self.refreshButton.clicked.connect(lambda: self.on_refresh_button_clicked())

        self.createButton.clicked.connect(lambda: self.on_create_button_clicked())
        # self.convertButton.clicked.connect(lambda: self.on_convert_button_clicked())
        self.backButton.clicked.connect(lambda: self.on_back_button_clicked())
        self.submitButton.clicked.connect(lambda: self.on_submit_button_clicked())
        self.projectsTable.cellDoubleClicked.connect(
            lambda: self.on_projects_table_cell_double_clicked()
        )

        self.buttonBox.button(QDialogButtonBox.Close).clicked.connect(
            lambda: self.on_button_box_clicked()
        )
        self.buttonBox.button(QDialogButtonBox.Reset).setText(self.tr("Logout"))
        self.buttonBox.button(QDialogButtonBox.Reset).setIcon(QIcon())
        self.buttonBox.button(QDialogButtonBox.Reset).clicked.connect(
            lambda: self.on_logout_button_clicked()
        )

        self.projectsTable.selectionModel().selectionChanged.connect(
            lambda: self.on_projects_table_selection_changed()
        )
        self.localDirLineEdit.textChanged.connect(
            lambda: self.on_local_dir_line_edit_text_changed()
        )
        self.localDirLineEdit.editingFinished.connect(
            lambda: self.on_local_dir_line_edit_editing_finished()
        )
        self.projectOwnerRefreshButton.clicked.connect(
            lambda: self.on_project_owner_refresh_button_clicked()
        )
        self.localDirButton.clicked.connect(lambda: self.on_local_dir_button_clicked())
        self.localDirButton.setMenu(QMenu())
        self.localDirButton.setPopupMode(QToolButton.MenuButtonPopup)
        self.localDirButton.menu().addAction(self.use_current_project_directory_action)

        self.network_manager.avatar_success.connect(lambda: self.update_welcome_label())
        self.network_manager.login_finished.connect(lambda: self.update_welcome_label())
        self.network_manager.logout_success.connect(lambda: self._on_logout_success())
        self.network_manager.logout_failed.connect(
            lambda err: self._on_logout_failed(err)
        )
        self.network_manager.projects_cache.projects_started.connect(
            lambda: self.on_projects_cached_projects_started()
        )
        self.network_manager.projects_cache.projects_error.connect(
            lambda err: self.on_projects_cached_projects_error(err)
        )
        self.network_manager.projects_cache.projects_updated.connect(
            lambda: self.on_projects_cached_projects_updated()
        )
        self.network_manager.projects_cache.project_files_started.connect(
            lambda project_id: self.on_projects_cached_project_files_started(project_id)
        )
        self.network_manager.projects_cache.project_files_error.connect(
            lambda project_id, error: self.on_projects_cached_project_files_error(
                project_id, error
            )
        )
        self.network_manager.projects_cache.project_files_updated.connect(
            lambda project_id: self.on_projects_cached_project_files_updated(project_id)
        )

        self.projectFilesTree.header().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.projectFilesTree.header().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.projectFilesTree.header().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.projectFilesTree.header().setSectionResizeMode(
            3, QHeaderView.ResizeToContents
        )

        self.update_ui_state()

    @property
    def current_cloud_project(self) -> Optional[CloudProject]:
        return self._current_cloud_project

    @current_cloud_project.setter
    def current_cloud_project(self, value: Optional[CloudProject]):
        if self._current_cloud_project == value:
            return

        self._current_cloud_project = value
        self.update_project_table_selection()
        self.update_ui_state()

    def on_auth_accepted(self):
        self.network_manager.projects_cache.refresh()
        self.update_welcome_label()

    def on_projects_cached_projects_started(self) -> None:
        self.projectsStack.setEnabled(False)
        self.feedbackLabel.setVisible(False)
        self.feedbackLabel.setText("")

    def on_projects_cached_projects_error(self, error: str) -> None:
        self.projectsStack.setEnabled(True)
        self.feedbackLabel.setVisible(True)
        self.feedbackLabel.setText(error)

    def on_projects_cached_projects_updated(self) -> None:
        self.projectsStack.setEnabled(True)
        self.projects_refreshed.emit()
        self.show_projects()

    def on_projects_cached_project_files_started(self, project_id: str) -> None:
        self.projectFilesTab.setEnabled(False)
        self.feedbackLabel.setVisible(False)
        self.feedbackLabel.setText("")

    def on_projects_cached_project_files_error(
        self, project_id: str, error: str
    ) -> None:
        self.projectFilesTab.setEnabled(True)

        if self.current_cloud_project and self.current_cloud_project.id != project_id:
            return

        self.feedbackLabel.setText(
            "Obtaining project files list failed: {}".format(error)
        )
        self.feedbackLabel.setVisible(True)

    def on_project_files_toggle_expand_button_clicked(self) -> None:
        should_expand = not self.projectFilesTree.topLevelItem(0).data(1, Qt.UserRole)
        self.projectFilesTree.topLevelItem(0).setData(1, Qt.UserRole, should_expand)

        for idx in range(self.projectFilesTree.topLevelItemCount()):
            self.expand_state(self.projectFilesTree.topLevelItem(idx), should_expand)

    def expand_state(self, item: QTreeWidgetItem, should_expand: bool) -> None:
        item.setExpanded(should_expand)

        for idx in range(item.childCount()):
            child = item.child(idx)

            if child.childCount() == 0:
                continue

            self.expand_state(child, should_expand)

    def on_projects_cached_project_files_updated(self, project_id: str) -> None:
        if (
            not self.current_cloud_project
            or self.current_cloud_project.id != project_id
        ):
            return

        self.projectFilesTab.setEnabled(True)

        # NOTE algorithmic part
        # ##########
        # The "cloud_files" objects are assumed to be sorted alphabetically by name.
        # First split filenames into parts. For example: '/home/ninja.file' will result into ['home', 'ninja.file'] parts.
        # Then store pairs of the part and the corresponding QTreeWidgetItem in a stack.
        # Pop and push to the stack when the current filename part does not match the previous one.
        # ##########
        stack = []

        for project_file in self.current_cloud_project.get_files(
            ProjectFileCheckout.Cloud
        ):
            assert isinstance(project_file.versions, list)

            parts = tuple(project_file.path.parts)
            for part_idx, part in enumerate(parts):
                if len(stack) > part_idx and stack[part_idx][0] == part:
                    continue
                else:
                    stack = stack[0:part_idx]

                item = QTreeWidgetItem()
                item.setText(0, part)

                stack.append((part, item))

                if len(stack) == 1:
                    self.projectFilesTree.addTopLevelItem(item)
                else:
                    stack[-2][1].addChild(stack[-1][1])

                # the length of the stack and the parts is equal for file entries
                if len(stack) == len(parts):
                    item.setToolTip(0, project_file.name)
                    item.setData(0, Qt.UserRole, project_file)

                    item.setText(1, str(project_file.size))
                    item.setTextAlignment(1, Qt.AlignRight)
                    item.setText(2, project_file.created_at)

                    versions_count = len(project_file.versions)
                    for version_idx, version_obj in enumerate(project_file.versions):
                        version_item = QTreeWidgetItem()

                        version_item.setData(0, Qt.UserRole, version_obj)
                        version_item.setText(
                            0, "Version {}".format(versions_count - version_idx)
                        )
                        version_item.setText(1, str(version_obj["size"]))
                        version_item.setTextAlignment(1, Qt.AlignRight)
                        version_item.setText(2, version_obj["last_modified"])

                        save_as_btn = QPushButton()
                        save_as_btn.setIcon(
                            QIcon(
                                str(
                                    Path(__file__).parent.joinpath(
                                        "../resources/cloud_download.svg"
                                    )
                                )
                            )
                        )
                        save_as_btn.clicked.connect(
                            self.on_save_as_btn_version_clicked(
                                project_file, version_idx
                            )
                        )
                        save_as_widget = QWidget()
                        save_as_layout = QHBoxLayout()
                        save_as_layout.setAlignment(Qt.AlignCenter)
                        save_as_layout.setContentsMargins(0, 0, 0, 0)
                        save_as_layout.addWidget(save_as_btn)
                        save_as_widget.setLayout(save_as_layout)

                        item.addChild(version_item)

                        self.projectFilesTree.setItemWidget(
                            version_item, 3, save_as_widget
                        )
                else:
                    item.setExpanded(True)

                    # TODO make a fancy button that marks all the child items as checked or not
        # NOTE END algorithmic part

    @closure
    def on_save_as_btn_version_clicked(
        self, project_file: ProjectFile, version_idx: int, _is_checked: bool
    ):
        assert project_file.versions
        assert version_idx < len(project_file.versions)

        basename = "{}_{}{}".format(
            project_file.path.stem, str(version_idx + 1), project_file.path.suffix
        )

        if project_file.local_path:
            default_path = project_file.local_path.parent.joinpath(basename)
        else:
            default_path = Path(QgsProject().instance().homePath()).joinpath(
                project_file.path.parent, basename
            )

        version_dest_filename, _ = QFileDialog.getSaveFileName(
            self, self.tr("Select version file name…"), str(default_path)
        )

        if not version_dest_filename:
            return

        def on_redirected_wrapper() -> Callable:
            def on_redirected(url: QUrl) -> None:
                reply = self.network_manager.get(url, version_dest_filename)
                reply.downloadProgress.connect(
                    lambda r, t: self.on_download_file_progress(
                        reply, r, t, project_file=project_file
                    )
                )
                reply.finished.connect(
                    lambda: self.on_download_file_finished(
                        reply, project_file=project_file
                    )
                )

            return on_redirected

        def on_finished_wrapper(reply: QNetworkReply) -> Callable:
            def on_finished():
                try:
                    self.network_manager.handle_response(reply, False)
                except CloudException as err:
                    self.feedbackLabel.setText(
                        self.tr(
                            'Downloading file "{}" failed: {}'.format(
                                project_file.name, str(err)
                            )
                        )
                    )
                    self.feedbackLabel.setVisible(True)
                    return

            return on_finished

        reply = self.network_manager.get_file_request(
            self.current_cloud_project.id + "/" + str(project_file.name) + "/",
            project_file.versions[version_idx]["version_id"],
        )

        reply.redirected.connect(on_redirected_wrapper())
        reply.finished.connect(on_finished_wrapper(reply))

    def on_download_file_progress(
        self,
        _reply: QNetworkReply,
        bytes_received: int,
        bytes_total: int,
        project_file: ProjectFile,
    ) -> None:
        self.feedbackLabel.setVisible(True)
        self.feedbackLabel.setText(
            self.tr('Downloading file "{}" at {}%…').format(
                project_file.name, int((bytes_received / bytes_total) * 100)
            )
        )

    def on_download_file_finished(
        self, reply: QNetworkReply, project_file: ProjectFile
    ) -> None:
        try:
            self.network_manager.handle_response(reply, False)
        except CloudException as err:
            self.feedbackLabel.setText(
                self.tr(
                    'Downloading file "{}" failed: {}'.format(
                        project_file.name, str(err)
                    )
                )
            )
            self.feedbackLabel.setVisible(True)
            return

        self.feedbackLabel.setVisible(False)
        self.feedbackLabel.setText("")

    def on_use_current_project_directory_action_triggered(self, _toggled: bool) -> None:
        self.localDirLineEdit.setText(str(Path(QgsProject.instance().homePath())))

    def on_button_box_clicked(self) -> None:
        self.close()

    def on_local_dir_line_edit_text_changed(self) -> None:
        local_dir = self.localDirLineEdit.text()
        self.submitButton.setEnabled(False)

        feedback, feedback_msg = self.local_dir_feedback(local_dir)
        self.localDirFeedbackLabel.setText(feedback_msg)

        if feedback == LocalDirFeedback.Error:
            self.localDirFeedbackLabel.setStyleSheet("color: red;")
        elif feedback == LocalDirFeedback.Warning:
            self.localDirFeedbackLabel.setStyleSheet("color: orange;")
        else:
            self.localDirFeedbackLabel.setStyleSheet("color: green;")

        self.submitButton.setEnabled(True)

    def on_local_dir_line_edit_editing_finished(self) -> None:
        local_dir = self.localDirLineEdit.text()

        if self.current_cloud_project:
            self.current_cloud_project.update_data({"local_dir": local_dir})

    def on_local_dir_button_clicked(self) -> None:
        dirname = self.select_local_dir()
        if dirname:
            self.localDirLineEdit.setText(str(Path(dirname)))

    def on_project_owner_refresh_button_clicked(self) -> None:
        self.request_refresh_project_owners_combobox()

    def on_get_user_organizations_finished(self, reply: QNetworkReply) -> None:
        try:
            payload = self.network_manager.json_array(reply)
            self.refresh_project_owners_combobox(payload)
        except Exception:
            self.feedbackLabel.setText(self.tr("Failed to refresh project owners."))

    def on_logout_button_clicked(self) -> None:
        self.buttonBox.button(QDialogButtonBox.Reset).setEnabled(False)
        self.feedbackLabel.setVisible(False)
        self.network_manager.logout()

    def on_refresh_button_clicked(self) -> None:
        self.network_manager.projects_cache.refresh()

    def refresh_project_owners_combobox(
        self, organizations: List[Dict[str, Any]] = []
    ) -> None:
        username = self.network_manager.auth().config("username")
        selected_value = username
        is_project_owner_select_enabled = True

        if self.current_cloud_project:
            selected_value = self.current_cloud_project.owner
            is_project_owner_select_enabled = can_change_project_owner(
                self.current_cloud_project
            )

        self.projectOwnerComboBox.clear()
        self.projectOwnerComboBox.setEnabled(is_project_owner_select_enabled)
        self.projectOwnerRefreshButton.setEnabled(is_project_owner_select_enabled)
        self.projectOwnerComboBox.addItem(username, username)

        if organizations:
            for org in organizations:
                self.projectOwnerComboBox.addItem(org["username"], org["username"])

        selected_value_idx = self.projectOwnerComboBox.findData(selected_value)
        if selected_value_idx == -1:
            selected_value_idx = 0
            self.projectOwnerComboBox.insertItem(
                selected_value_idx,
                selected_value,
                selected_value,
            )

        self.projectOwnerComboBox.setCurrentIndex(selected_value_idx)

    def request_refresh_project_owners_combobox(self) -> None:
        self.projectOwnerRefreshButton.setEnabled(False)

        reply = self.network_manager.get_user_organizations(
            self.network_manager.auth().config("username")
        )
        reply.finished.connect(lambda: self.on_get_user_organizations_finished(reply))

    def local_dir_feedback(
        self, local_dir, empty_ok=True, exiting_ok=True
    ) -> Tuple[LocalDirFeedback, str]:
        if not local_dir:
            return LocalDirFeedback.Error, self.tr(
                "Please select local directory where the project to be stored."
            )
        elif not Path(local_dir).is_dir():
            return LocalDirFeedback.Warning, self.tr(
                "The entered path is not an existing directory. It will be created after you submit this form."
            )
        elif len(get_qgis_files_within_dir(Path(local_dir))) == 0:
            message = self.tr(
                "The entered path does not contain a QGIS project file yet."
            )
            status = LocalDirFeedback.Warning

            if empty_ok:
                status = LocalDirFeedback.Success
                message += " "
                message += self.tr("You can always add one later.")

            return status, message
        elif len(get_qgis_files_within_dir(Path(local_dir))) == 1:
            message = self.tr("The entered path contains one QGIS project file.")
            status = LocalDirFeedback.Warning

            if exiting_ok:
                status = LocalDirFeedback.Success
                message += " "
                message += self.tr("Exactly as it should be.")

            return status, message
        else:
            return LocalDirFeedback.Error, self.tr(
                "Multiple project files have been found in the directory. Please leave exactly one QGIS project in the root directory."
            )

    def show_projects(self) -> None:
        self.feedbackLabel.setText("")
        self.feedbackLabel.setVisible(False)

        self.projectsTable.setRowCount(0)
        self.projectsTable.setSortingEnabled(False)

        if self.network_manager.projects_cache.projects is None:
            self.network_manager.projects_cache.refresh()
            return

        for cloud_project in self.network_manager.projects_cache.projects:
            if (
                self.projectsType.currentIndex() != 1
                and cloud_project.user_role_origin == "public"
            ) or (
                self.projectsType.currentIndex() == 1
                and cloud_project.user_role_origin != "public"
            ):
                continue

            count = self.projectsTable.rowCount()
            self.projectsTable.insertRow(count)

            item = QTableWidgetItem(cloud_project.name)
            item.setData(Qt.UserRole, cloud_project)
            item.setData(Qt.EditRole, cloud_project.name)

            cbx_local = QCheckBox()
            cbx_local.setEnabled(False)
            cbx_local.setChecked(bool(cloud_project.local_dir))
            # # it's more UI friendly when the checkbox is centered, an ugly workaround to achieve it
            cbx_local_widget = QWidget()
            cbx_local_layout = QHBoxLayout()
            cbx_local_layout.setAlignment(Qt.AlignCenter)
            cbx_local_layout.setContentsMargins(0, 0, 0, 0)
            cbx_local_layout.addWidget(cbx_local)
            cbx_local_widget.setLayout(cbx_local_layout)
            if bool(cloud_project.local_dir):
                cbx_local_widget.setToolTip(str(cloud_project.local_dir))
            else:
                cbx_local_widget.setToolTip(self.tr("No local dir configured"))

            self.projectsTable.setItem(count, 0, item)
            self.projectsTable.setItem(count, 1, QTableWidgetItem(cloud_project.owner))
            self.projectsTable.setCellWidget(count, 2, cbx_local_widget)

        self.projectsTable.sortByColumn(1, Qt.AscendingOrder)
        self.projectsTable.sortByColumn(0, Qt.AscendingOrder)
        self.projectsTable.setSortingEnabled(True)
        self.update_project_table_selection()

    def sync(self) -> None:
        assert self.current_cloud_project is not None
        self.show_sync_popup()

    def launch(self) -> None:
        assert self.current_cloud_project is not None

        if self.current_cloud_project.local_dir is None:
            self.sync()
            return

        if self.current_cloud_project.cloud_files is not None:
            project_filename = self.current_cloud_project.local_project_file

            # no local project name found
            if not project_filename:
                iface.messageBar().pushInfo(
                    f'QFieldSync "{self.current_cloud_project.name}":',
                    self.tr(
                        "Cannot find local project file. QFieldSync will first download the project."
                    ),
                )
                return

            # it is the current project, no need to reload
            if str(project_filename.local_path) == QgsProject().instance().fileName():
                iface.messageBar().pushInfo(
                    f'QFieldSync "{self.current_cloud_project.name}":',
                    self.tr("Already loaded the selected project."),
                )
                return

            iface.addProject(str(project_filename.local_path))

            self.update_project_table_selection()
            self.update_ui_state()

            return

        reply = self.network_manager.projects_cache.get_project_files(
            self.current_cloud_project.id
        )
        reply.finished.connect(lambda: self.launch())

    def select_local_dir(self) -> Optional[str]:
        """
        ```
            if there is saved location for this project id #
                upload all the files (or the missing on the cloud only)
                download all the files (or the missing on the local only) #
                if project is the current one: #
                    reload the project #
            else
                if the cloud project is not empty
                    ask for path that is empty dir
                    download the project there
                else
                    ask for path #

                    if path contains .qgs file: #
                        assert single .qgs file #
                        upload all the files #

                save the project location #

            ask should that project be opened
        ```
        """

        local_dir = None
        initial_path = (
            self.localDirLineEdit.text()
            or str(Path(QgsProject.instance().homePath()).parent)
            or self.preferences.value("cloudDirectory")
        )

        # cloud project is empty, you can upload a local project into it
        if self.current_cloud_project is None or (
            self.current_cloud_project.cloud_files is not None
            and len(self.current_cloud_project.cloud_files) == 0
        ):
            while local_dir is None:
                local_dir = QFileDialog.getExistingDirectory(
                    self, self.tr("Upload local project to QFieldCloud…"), initial_path
                )

                if local_dir == "":
                    return

                feedback, feedback_msg = self.local_dir_feedback(
                    local_dir, empty_ok=False
                )
                title = self.tr("Cannot upload local QFieldSync directory")

                # all is good, we can continue
                if feedback == LocalDirFeedback.Success:
                    break

                if feedback == LocalDirFeedback.Error:
                    QMessageBox.critical(self, title, feedback_msg)
                elif feedback == LocalDirFeedback.Warning:
                    QMessageBox.warning(self, title, feedback_msg)

                local_dir = None
                continue

            return local_dir

        # cloud project exists and has files in it, so checkout in an empty dir
        else:
            assert self.current_cloud_project

            while local_dir is None:
                local_dir = QFileDialog.getExistingDirectory(
                    self, self.tr("Save QFieldCloud project to…"), initial_path
                )

                if local_dir == "":
                    return

                # when the dir is empty, all is good. But if not there are some file, we need to ask the user to confirm what to do
                if list(Path(local_dir).iterdir()):
                    buttons = QMessageBox.Ok | QMessageBox.Abort
                    feedback, feedback_msg = self.local_dir_feedback(
                        local_dir, exiting_ok=False
                    )
                    title = self.tr("QFieldSync checkout prefers an empty directory")
                    answer = None

                    if feedback == LocalDirFeedback.Error:
                        answer = QMessageBox.critical(
                            self, title, feedback_msg, buttons
                        )
                    elif feedback == LocalDirFeedback.Warning:
                        answer = QMessageBox.warning(self, title, feedback_msg, buttons)

                    if answer == QMessageBox.Abort:
                        local_dir = None
                        continue

                break

        return local_dir

    def on_project_sync_button_clicked(self) -> None:
        self.sync()

    def on_project_edit_button_clicked(self) -> None:
        self.show_project_form()

    def on_project_launch_button_clicked(self) -> None:
        self.launch()

    def on_project_delete_button_clicked(self) -> None:
        button_pressed = QMessageBox.question(
            self,
            self.tr("Delete QFieldCloud project"),
            self.tr(
                'Are you sure you want to delete the QFieldCloud project "{}"? Nevertheless, your local files will remain.'
            ).format(self.current_cloud_project.name),
        )

        if button_pressed != QMessageBox.Yes:
            return

        self.projectsStack.setEnabled(False)

        reply = self.network_manager.delete_project(self.current_cloud_project.id)
        reply.finished.connect(lambda: self.on_delete_project_reply_finished(reply))

    def on_delete_project_reply_finished(self, reply: QNetworkReply) -> None:
        self.projectsStack.setEnabled(True)

        try:
            self.network_manager.handle_response(reply, False)
        except CloudException as err:
            self.feedbackLabel.setText(
                self.tr("Project delete failed: {}").format(str(err))
            )
            self.feedbackLabel.setVisible(True)
            return

        self.network_manager.projects_cache.refresh()

    def on_projects_table_cell_double_clicked(self) -> None:
        self.show_project_form()

    def on_create_button_clicked(self) -> None:
        self.projectsTable.clearSelection()
        # self.current_cloud_project = None
        self.projectsStack.setCurrentWidget(self.projectCreatePage)
        self.createProjectWidget.restart()
        # self.projectCreatePage.setWidget()
        # self.show_project_form()

    def on_convert_button_clicked(self) -> None:
        if QgsProject.instance().mapLayers():
            self.cloud_convert_dlg = CloudConverterDialog(
                iface,
                self.network_manager,
                QgsProject.instance(),
                self,
            )
            self.cloud_convert_dlg.setAttribute(Qt.WA_DeleteOnClose)
            self.cloud_convert_dlg.setWindowFlags(
                self.cloud_convert_dlg.windowFlags() | Qt.Tool
            )
            self.cloud_convert_dlg.open()
            self.update_ui_state()
        else:
            iface.messageBar().pushMessage(
                self.tr("At least one layer is required to convert a project."),
                Qgis.Warning,
                5,
            )

    def show_project_form(self) -> None:
        self.show()

        self.projectsStack.setCurrentWidget(self.projectsFormPage)
        self.projectTabs.setCurrentWidget(self.projectFormTab)
        self.projectFilesTree.clear()
        self.projectNameLineEdit.setEnabled(True)
        self.projectDescriptionTextEdit.setEnabled(True)
        self.projectIsPrivateCheckBox.setEnabled(True)
        self.projectOwnerComboBox.setEnabled(True)

        self.refresh_project_owners_combobox()
        self.request_refresh_project_owners_combobox()

        if self.current_cloud_project is None:
            self.submitButton.setText(self.tr("Create new project"))
            self.projectTabs.setTabEnabled(1, False)
            self.projectTabs.setTabEnabled(2, False)
            self.projectNameLineEdit.setText(
                to_cloud_title(QgsProject.instance().title())
            )
            self.projectDescriptionTextEdit.setPlainText("")
            self.projectIsPrivateCheckBox.setChecked(True)

            # check if there is already another cloud project using the currently open filename
            if CloudProject.get_cloud_project_id(QgsProject.instance().homePath()):
                self.localDirLineEdit.setText("")
            else:
                self.localDirLineEdit.setText(
                    str(Path(QgsProject().instance().homePath()))
                )

        else:
            self.submitButton.setText(self.tr("Update project details"))
            self.projectTabs.setTabEnabled(1, True)
            self.projectTabs.setTabEnabled(2, True)
            # TODO validate project name to match QFieldCloudRequirements
            self.projectNameLineEdit.setText(self.current_cloud_project.name)
            self.projectDescriptionTextEdit.setPlainText(
                self.current_cloud_project.description
            )
            self.projectIsPrivateCheckBox.setChecked(
                self.current_cloud_project.is_private
            )
            self.localDirLineEdit.setText(self.current_cloud_project.local_dir)
            self.projectUrlLabelValue.setText(
                '<a href="{url}">{url}</a>'.format(
                    url=(self.network_manager.url + self.current_cloud_project.url)
                )
            )
            self.createdAtLabelValue.setText(
                QDateTime.fromString(
                    self.current_cloud_project.created_at, Qt.ISODateWithMs
                ).toString()
            )
            self.updatedAtLabelValue.setText(
                QDateTime.fromString(
                    self.current_cloud_project.updated_at, Qt.ISODateWithMs
                ).toString()
            )
            self.lastSyncedAtLabelValue.setText(
                QDateTime.fromString(
                    self.current_cloud_project.updated_at, Qt.ISODateWithMs
                ).toString()
            )

            if self.current_cloud_project.user_role not in ("admin", "manager"):
                self.projectNameLineEdit.setEnabled(False)
                self.projectDescriptionTextEdit.setEnabled(False)
                self.projectIsPrivateCheckBox.setEnabled(False)
                self.projectOwnerComboBox.setEnabled(False)

            self.network_manager.projects_cache.get_project_files(
                self.current_cloud_project.id
            )

    def on_back_button_clicked(self) -> None:
        self.projectsStack.setCurrentWidget(self.projectsListPage)

    def on_submit_button_clicked(self) -> None:
        cloud_project_data = {
            "name": self.projectNameLineEdit.text(),
            "description": self.projectDescriptionTextEdit.toPlainText(),
            "owner": self.projectOwnerComboBox.currentData(),
            "private": self.projectIsPrivateCheckBox.isChecked(),
            "local_dir": self.localDirLineEdit.text(),
        }

        if (
            self.projectNameLineEdit.validator().validate(
                cloud_project_data["name"], 0
            )[0]
            != QValidator.Acceptable
        ):
            QMessageBox.warning(
                None,
                self.tr("Invalid project name"),
                self.tr(
                    "You cannot create a new project without setting a valid name first."
                ),
            )
            return

        self.projectsFormPage.setEnabled(False)
        self.feedbackLabel.setVisible(True)

        if self.current_cloud_project is None:
            self.feedbackLabel.setText(self.tr("Creating project…"))
            reply = self.network_manager.create_project(
                cloud_project_data["name"],
                cloud_project_data["owner"],
                cloud_project_data["description"],
                cloud_project_data["private"],
            )
            reply.finished.connect(
                lambda: self.on_create_project_finished(
                    reply, local_dir=cloud_project_data["local_dir"]
                )
            )
        else:
            self.current_cloud_project.update_data(cloud_project_data)
            self.feedbackLabel.setText(self.tr("Updating project…"))

            reply = self.network_manager.update_project(
                self.current_cloud_project.id,
                self.current_cloud_project.name,
                self.current_cloud_project.owner,
                self.current_cloud_project.description,
                self.current_cloud_project.is_private,
            )
            reply.finished.connect(lambda: self.on_update_project_finished(reply))

    def on_create_project_finished(
        self, reply: QNetworkReply, local_dir: str = None
    ) -> None:
        self.projectsFormPage.setEnabled(True)

        try:
            payload = self.network_manager.json_object(reply)
        except CloudException as err:
            self.feedbackLabel.setText("Project create failed: {}".format(str(err)))
            self.feedbackLabel.setVisible(True)
            return

        # save `local_dir` configuration permanently, `CloudProject` constructor does this for free
        project = CloudProject(
            {
                **payload,
                "local_dir": local_dir,
            }
        )

        self.projectsStack.setCurrentWidget(self.projectsListPage)
        self.feedbackLabel.setVisible(False)

        reply = self.network_manager.projects_cache.refresh()
        reply.finished.connect(
            lambda: self.on_create_project_finished_projects_refreshed(
                reply, project.id
            )
        )

    def update_welcome_label(self) -> None:
        if self.network_manager.has_token():
            avatar_filename = self.network_manager.user_details.get("avatar_filename")
            if avatar_filename:
                self.avatarLabel.setVisible(True)
                pixmap = rounded_pixmap(avatar_filename, self.avatarLabel.height())
                self.avatarLabel.setPixmap(pixmap)
            else:
                self.avatarLabel.setVisible(False)
                self.avatarLabel.setPixmap(QPixmap())

            self.welcomeLabel.setText(
                self.tr("Greetings {}.").format(
                    f'<a href="{self.network_manager.url}">{self.network_manager.auth().config("username")}</a>'
                )
            )

            if self.network_manager.url == self.network_manager.server_urls()[0]:
                self.welcomeLabel.setToolTip(
                    self.tr("You are logged in with the following username")
                )
            else:
                self.welcomeLabel.setToolTip(
                    self.tr(
                        "You are logged in with the following username at {}"
                    ).format(self.network_manager.url)
                )
        else:
            self.avatarLabel.setVisible(False)
            self.welcomeLabel.setText("Logged out.")
            self.welcomeLabel.setToolTip("")

    def update_ui_state(self) -> None:
        if (
            self.network_manager.projects_cache.currently_open_project
            or self.network_manager.projects_cache.is_currently_open_project_cloud_local
        ):
            pass
            # self.convertButton.setEnabled(False)
        else:
            pass
            # self.convertButton.setEnabled(True)

    def update_project_table_selection(self) -> None:
        font = QFont()

        for row_idx in range(self.projectsTable.rowCount()):
            cloud_project = self.projectsTable.item(row_idx, 0).data(Qt.UserRole)
            is_currently_open_project = (
                cloud_project
                == self.network_manager.projects_cache.currently_open_project
            )

            font.setBold(is_currently_open_project)

            self.projectsTable.item(row_idx, 0).setFont(font)
            self.projectsTable.item(row_idx, 1).setFont(font)

            if cloud_project == self.current_cloud_project:
                index = self.projectsTable.model().index(row_idx, 0)
                self.projectsTable.setCurrentIndex(index)
                self.projectsTable.selectionModel().select(
                    index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
                )

            self.update_project_buttons()

    def on_create_project_finished_projects_refreshed(
        self, reply: QNetworkReply, project_id: str
    ) -> None:
        error = from_reply(reply)

        if error:
            iface.messageBar().pushWarning(
                "QFieldSync",
                self.tr("Failed to refresh the project list, please do it manually."),
            )
            return

        cloud_project = self.network_manager.projects_cache.find_project(project_id)
        self.current_cloud_project = cloud_project

        self.launch()
        self.sync()

    def on_update_project_finished(self, reply: QNetworkReply) -> None:
        self.projectsFormPage.setEnabled(True)

        try:
            self.network_manager.json_object(reply)
        except CloudException as err:
            self.feedbackLabel.setText("Project update failed: {}".format(str(err)))
            self.feedbackLabel.setVisible(True)
            return

        self.projectsStack.setCurrentWidget(self.projectsListPage)
        self.feedbackLabel.setVisible(False)

        self.network_manager.projects_cache.refresh()

    def on_projects_table_selection_changed(self) -> None:
        self.update_project_buttons()

    def update_project_buttons(self) -> None:
        has_selection = False
        is_currently_open_project = False
        can_delete_selected_project = False
        if self.projectsTable.selectionModel().hasSelection():
            has_selection = True
            row_idx = self.projectsTable.currentRow()
            self.current_cloud_project = self.projectsTable.item(row_idx, 0).data(
                Qt.UserRole
            )
            assert self.current_cloud_project

            is_currently_open_project = (
                self.current_cloud_project
                == self.network_manager.projects_cache.currently_open_project
            )
            can_delete_selected_project = can_delete_project(self.current_cloud_project)
            root_project_files = self.current_cloud_project.root_project_files
            if len(root_project_files) == 1:
                self.openButton.setToolTip(
                    self.tr('Open Project "{}"').format(root_project_files[0])
                )
            elif len(root_project_files) == 0:
                self.openButton.setToolTip(
                    self.tr(
                        "Cannot open project since no local .qgs or .qgz project file found"
                    )
                )
            else:
                self.openButton.setToolTip(
                    self.tr(
                        "Multiple .qgs or .qgz project files found in the project directory"
                    )
                )

        self.synchronizeButton.setEnabled(has_selection)
        self.editButton.setEnabled(has_selection)
        self.openButton.setEnabled(has_selection and not is_currently_open_project)
        self.deleteButton.setEnabled(has_selection and can_delete_selected_project)

    def show_sync_popup(self) -> None:
        assert self.current_cloud_project is not None, "No project to download selected"

        self.transfer_dialog = CloudTransferDialog(
            self.network_manager, self.current_cloud_project, self
        )
        self.transfer_dialog.rejected.connect(self.on_transfer_dialog_rejected)
        self.transfer_dialog.accepted.connect(self.on_transfer_dialog_accepted)
        self.transfer_dialog.open()

    def on_transfer_dialog_rejected(self) -> None:
        if self.project_transfer:
            self.project_transfer.abort_requests()

        if self.transfer_dialog:
            self.transfer_dialog.close()

        self.project_transfer = None
        self.transfer_dialog = None

    def on_transfer_dialog_accepted(self) -> None:
        QgsProject().instance().reloadAllLayers()
        self.show_projects()

        if self.transfer_dialog:
            self.transfer_dialog.close()

        self.project_transfer = None
        self.transfer_dialog = None

    def _on_logout_success(self) -> None:
        self.projectsTable.setRowCount(0)

        self.close()

    def _on_logout_failed(self, err: str) -> None:
        self.feedbackLabel.setText("Logout failed: {}".format(str(err)))
        self.feedbackLabel.setVisible(True)
        self.buttonBox.button(QDialogButtonBox.Reset).setEnabled(True)
