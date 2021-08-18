# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QFieldCloudConverterDialog
                                 A QGIS plugin
 Sync your projects to QField on android
                             -------------------
        begin                : 2021-057-22
        git sha              : $Format:%H$
        copyright            : (C) 2015 by OPENGIS.ch
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

import os
import re
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import pyqtSignal
from qgis.core import Qgis, QgsProject, QgsProviderRegistry
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QMessageBox, QWidget
from qgis.PyQt.uic import loadUiType

from qfieldsync.core.cloud_api import (
    CloudException,
    CloudNetworkAccessManager,
    from_reply,
)
from qfieldsync.core.cloud_converter import CloudConverter
from qfieldsync.core.cloud_project import CloudProject
from qfieldsync.core.cloud_transferrer import CloudTransferrer
from qfieldsync.core.preferences import Preferences
from qfieldsync.gui.cloud_login_dialog import CloudLoginDialog
from qfieldsync.libqfieldsync.utils.file_utils import (
    fileparts,
    get_unique_empty_dirname,
)
from qfieldsync.utils.qgis_utils import get_qgis_files_within_dir

WidgetUi, _ = loadUiType(
    os.path.join(os.path.dirname(__file__), "../ui/cloud_create_project_widget.ui")
)


class CloudConverterDialog(QWidget, WidgetUi):
    finished = pyqtSignal()

    def __init__(
        self,
        iface: QgisInterface,
        network_manager: CloudNetworkAccessManager,
        project: QgsProject,
        parent: QWidget = None,
    ) -> None:
        """Constructor."""
        super(CloudConverterDialog, self).__init__(parent=parent)
        self.setupUi(self)

        self.iface = iface
        self.project = project
        self.qfield_preferences = Preferences()
        self.network_manager = network_manager
        self.cloud_transferrer: Optional[CloudTransferrer] = None

        if not self.network_manager.has_token():
            CloudLoginDialog.show_auth_dialog(
                self.network_manager, lambda: self.close(), None, parent=self
            )
        else:
            self.network_manager.projects_cache.refresh()

        self.nextButton.clicked.connect(self.on_next_button_clicked)
        self.backButton.clicked.connect(self.on_back_button_clicked)
        self.createButton.clicked.connect(self.on_cloudify_button_clicked)

    def restart(self):
        self.stackedWidget.setCurrentWidget(self.selectTypePage)

    def cloudify_project(self):
        assert self.network_manager.projects_cache.projects

        for cloud_project in self.network_manager.projects_cache.projects:
            if cloud_project.name == self.get_cloud_project_name():
                QMessageBox.warning(
                    None,
                    self.tr("Warning"),
                    self.tr(
                        "The project name is already present in your QFieldCloud repository, please pick a different name."
                    ),
                )
                return

        if get_qgis_files_within_dir(self.dirnameLineEdit.text()):
            QMessageBox.warning(
                None,
                self.tr("Warning"),
                self.tr(
                    "The export directory already contains a project file, please pick a different directory."
                ),
            )
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)

        self.stackedWidget.setCurrentWidget(self.progressPage)

        if not self.project.title():
            self.project.setTitle(self.get_cloud_project_name())
            self.project.setDirty()

        cloud_convertor = CloudConverter(self.project, self.dirnameLineEdit.text())

        cloud_convertor.warning.connect(self.on_show_warning)
        cloud_convertor.total_progress_updated.connect(self.on_update_total_progressbar)

        try:
            cloud_convertor.convert()
        except Exception:
            QApplication.restoreOverrideCursor()
            critical_message = self.tr(
                "The project could not be converted into the export directory."
            )
            self.iface.messageBar().pushMessage(critical_message, Qgis.Critical, 0)
            self.close()
            return

        self.create_cloud_project()

    def get_cloud_project_name(self) -> str:
        pattern = re.compile(r"[\W_]+")
        return pattern.sub("", self.projectNameLineEdit.text())

    def create_cloud_project(self):
        if not self.project.title():
            self.project.setTitle(self.get_cloud_project_name())
            self.project.setDirty()

        reply = self.network_manager.create_project(
            self.get_cloud_project_name(),
            self.network_manager.auth().config("username"),
            self.project.metadata().abstract(),
            True,
        )
        reply.finished.connect(lambda: self.on_create_project_finished(reply))

    def on_create_project_finished(self, reply):
        try:
            payload = self.network_manager.json_object(reply)
        except CloudException as err:
            QApplication.restoreOverrideCursor()
            critical_message = self.tr(
                "QFieldCloud rejected projection creation: {}"
            ).format(from_reply(err.reply))
            self.iface.messageBar().pushMessage(critical_message, Qgis.Critical, 0)
            self.close()
            return

        # save `local_dir` configuration permanently, `CloudProject` constructor does this for free
        cloud_project = CloudProject(
            {**payload, "local_dir": self.dirnameLineEdit.text()}
        )

        self.cloud_transferrer = CloudTransferrer(self.network_manager, cloud_project)
        self.cloud_transferrer.upload_progress.connect(
            self.on_transferrer_update_progress
        )
        self.cloud_transferrer.finished.connect(self.on_transferrer_finished)
        self.cloud_transferrer.sync(list(cloud_project.files_to_sync), [], [])

    def do_post_cloud_convert_action(self):
        QApplication.restoreOverrideCursor()

        self.network_manager.projects_cache.refresh()

        result_message = self.tr(
            "Finished converting the project to QFieldCloud, you are now view its locally stored copy."
        )
        self.iface.messageBar().pushMessage(result_message, Qgis.Success, 0)
        self.finished.emit()

    def update_info_visibility(self):
        """
        Show the info label if there are unconfigured layers
        """
        pathResolver = QgsProject.instance().pathResolver()
        localizedDataPathLayers = []
        for layer in list(self.project.mapLayers().values()):
            if layer.dataProvider() is not None:
                metadata = QgsProviderRegistry.instance().providerMetadata(
                    layer.dataProvider().name()
                )
                if metadata is not None:
                    decoded = metadata.decodeUri(layer.source())
                    if "path" in decoded:
                        path = pathResolver.writePath(decoded["path"])
                        if path.startswith("localized:"):
                            localizedDataPathLayers.append(
                                "- {} ({})".format(layer.name(), path[10:])
                            )

        if localizedDataPathLayers:
            if len(localizedDataPathLayers) == 1:
                self.infoLocalizedLayersLabel.setText(
                    self.tr("The layer stored in a localized data path is:\n{}").format(
                        "\n".join(localizedDataPathLayers)
                    )
                )
            else:
                self.infoLocalizedLayersLabel.setText(
                    self.tr(
                        "The layers stored in a localized data path are:\n{}"
                    ).format("\n".join(localizedDataPathLayers))
                )
            self.infoLocalizedLayersLabel.setVisible(True)
            self.infoLocalizedPresentLabel.setVisible(True)
        else:
            self.infoLocalizedLayersLabel.setVisible(False)
            self.infoLocalizedPresentLabel.setVisible(False)
        self.infoGroupBox.setVisible(len(localizedDataPathLayers) > 0)

    def get_unique_project_name(self, project: QgsProject) -> str:
        project_name = project.baseName()
        if not project_name:
            project_name = "CloudProject"

        pattern = re.compile(r"[\W_]+")
        project_name = pattern.sub("", project_name)
        project_name = (
            self.network_manager.projects_cache.get_unique_name(project_name) or ""
        )

        return project_name

    def on_update_total_progressbar(self, current, layer_count, message):
        self.totalProgressBar.setMaximum(layer_count)
        self.totalProgressBar.setValue(current)

    def on_transferrer_update_progress(self, fraction):
        self.uploadProgressBar.setMaximum(100)
        self.uploadProgressBar.setValue(int(fraction * 100))

    def on_transferrer_finished(self):
        self.do_post_cloud_convert_action()

    def on_show_warning(self, _, message):
        self.iface.messageBar().pushMessage(message, Qgis.Warning, 0)

    def on_next_button_clicked(self) -> None:
        if self.cloudifyRadioButton.isChecked():
            self.stackedWidget.setCurrentWidget(self.projectDetailsPage)
        elif self.createCloudRadioButton.isChecked():
            self.stackedWidget.setCurrentWidget(self.projectDetailsPage)

        project_name = self.get_unique_project_name(self.project)
        self.projectNameLineEdit.setText(project_name)
        self.projectDescriptionTextEdit.setText(self.project.metadata().abstract())

        project_filename = (
            project_name.lower()
            if project_name
            else fileparts(QgsProject.instance().fileName())[1]
        )
        export_dirname = get_unique_empty_dirname(
            Path(self.qfield_preferences.value("cloudDirectory")).joinpath(
                project_filename
            )
        )
        self.dirnameLineEdit.setText(str(export_dirname))

        self.update_info_visibility()

    def on_back_button_clicked(self):
        self.stackedWidget.setCurrentWidget(self.selectTypePage)

    def on_cloudify_button_clicked(self):
        if self.cloudifyRadioButton.isChecked():
            self.cloudify_project()
        elif self.createCloudRadioButton.isChecked():
            self.create_cloud_project()
