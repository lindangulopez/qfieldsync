# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QFieldSync
                             -------------------
        begin                : 2020-08-19
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


import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import QUrl
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply

from qfieldsync.core.cloud_api import CloudNetworkAccessManager
from qfieldsync.core.cloud_project import CloudProject, ProjectFile, ProjectFileCheckout
from qfieldsync.libqfieldsync.utils.file_utils import copy_multifile


class CloudTransferrer(QObject):
    # TODO show progress of individual files
    progress = pyqtSignal(float)
    error = pyqtSignal(str, Exception)
    abort = pyqtSignal()
    finished = pyqtSignal()
    upload_progress = pyqtSignal(float)
    download_progress = pyqtSignal(float)
    upload_finished = pyqtSignal()
    download_finished = pyqtSignal()
    delete_finished = pyqtSignal()

    def __init__(
        self, network_manager: CloudNetworkAccessManager, cloud_project: CloudProject
    ) -> None:
        super(CloudTransferrer, self).__init__(parent=None)
        assert cloud_project.local_dir

        self.network_manager = network_manager
        self.cloud_project = cloud_project
        self._files_to_upload = {}
        self._files_to_download: Dict[str, ProjectFile] = {}
        self._files_to_delete = {}
        self.upload_files_finished = 0
        self.upload_bytes_total_files_only = 0
        self.total_download_bytes = 0
        self.delete_files_finished = 0
        self.is_aborted = False
        self.is_started = False
        self.is_finished = False
        self.is_upload_active = False
        self.is_download_active = False
        self.is_delete_active = False
        self.is_project_list_update_active = False
        self.replies = []
        self.temp_dir = Path(cloud_project.local_dir).joinpath(".qfieldsync")

        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

        self.temp_dir.mkdir()
        self.temp_dir.joinpath("backup").mkdir()
        self.temp_dir.joinpath("upload").mkdir()
        self.temp_dir.joinpath("download").mkdir()

        self.upload_finished.connect(self._on_upload_finished)
        self.delete_finished.connect(self._on_delete_finished)
        self.download_finished.connect(self._on_download_finished)

        self.network_manager.logout_success.connect(self._on_logout_success)

    def sync(
        self,
        files_to_upload: List[ProjectFile],
        files_to_download: List[ProjectFile],
        files_to_delete: List[ProjectFile],
    ) -> None:
        assert not self.is_started

        self.is_started = True

        # prepare the files to be uploaded, copy them in a temporary destination
        for project_file in files_to_upload:
            assert project_file.local_path

            project_file.flush()

            filename = project_file.name
            temp_filename = self.temp_dir.joinpath("upload", filename)
            temp_filename.parent.mkdir(parents=True, exist_ok=True)
            copy_multifile(project_file.local_path, temp_filename)

            self.upload_bytes_total_files_only += project_file.local_size or 0
            self._files_to_upload[filename] = {
                "project_file": project_file,
                "bytes_total": project_file.local_size,
                "bytes_transferred": 0,
                "temp_filename": temp_filename,
            }

        # prepare the files to be delete, both locally and remotely
        for project_file in files_to_delete:
            filename = project_file.name

            self._files_to_delete[filename] = {
                "project_file": project_file,
            }

        # prepare the files to be downloaded, download them in a temporary destination
        for project_file in files_to_download:
            filename = project_file.name

            temp_filename = self.temp_dir.joinpath("download", filename)
            temp_filename.parent.mkdir(parents=True, exist_ok=True)

            self.total_download_bytes += project_file.size or 0
            self._files_to_download[filename] = project_file

        self._make_backup()
        self._upload()

    def _upload(self) -> None:
        assert not self.is_upload_active, "Upload in progress"
        assert not self.is_download_active, "Download in progress"

        self.is_upload_active = True

        # nothing to upload
        if len(self._files_to_upload) == 0:
            self.upload_progress.emit(1)
            self.upload_finished.emit()
            # NOTE check _on_upload_finished
            return

        assert self.cloud_project.local_dir

        for filename in self._files_to_upload:
            temp_filename = self._files_to_upload[filename]["temp_filename"]

            reply = self.network_manager.cloud_upload_files(
                "files/" + self.cloud_project.id + "/" + filename,
                filenames=[str(temp_filename)],
            )
            reply.uploadProgress.connect(
                self._on_upload_file_progress_wrapper(reply, filename)
            )
            reply.finished.connect(
                self._on_upload_file_finished_wrapper(reply, filename)
            )

            self.replies.append(reply)

    def _delete(self) -> None:
        self.is_delete_active = True

        # nothing to delete
        if len(self._files_to_delete) == 0:
            self.delete_finished.emit()
            # NOTE check _on_delete_finished
            return

        for filename in self._files_to_delete:
            project_file = self._files_to_delete[filename]["project_file"]

            if project_file.checkout == ProjectFileCheckout.Local:
                self.delete_files_finished += 1
                Path(project_file.local_path).unlink()

            if project_file.checkout & ProjectFileCheckout.Cloud:
                if project_file.checkout & ProjectFileCheckout.Local:
                    Path(project_file.local_path).unlink()

                reply = self.network_manager.delete_file(
                    self.cloud_project.id + "/" + str(filename) + "/"
                )
                reply.finished.connect(
                    self._on_delete_file_finished_wrapper(reply, filename)
                )

        # in case all the files to delete were local only
        if self.delete_files_finished == len(self._files_to_delete):
            self.delete_finished.emit()

    def _download(self) -> None:
        assert not self.is_upload_active, "Upload in progress"
        assert not self.is_download_active, "Download in progress"

        self.is_download_active = True

        # nothing to download
        if len(self._files_to_download) == 0:
            self.download_progress.emit(1)
            self.download_finished.emit()
            return

        self.throttled_downloader = ThrottledFileTransferrer(
            self.network_manager,
            self.cloud_project,
            list(self._files_to_download.keys()),
        )
        self.throttled_downloader.download()
        self.throttled_downloader.error.connect(self._on_throttled_download_error)
        self.throttled_downloader.progress.connect(self._on_throttled_download_progress)
        self.throttled_downloader.finished.connect(self._on_throttled_download_finished)

    def _on_throttled_download_progress(
        self, filename: str, bytes_transferred: int, _bytes_total: int
    ) -> None:
        fraction = min(bytes_transferred / max(self.total_download_bytes, 1), 1)
        self.download_progress.emit(fraction)

    def _on_throttled_download_error(self, filename: str, error: str) -> None:
        self.throttled_downloader.abort()

    def _on_throttled_download_finished(self) -> None:
        self._temp_dir2main_dir("download")
        self.download_progress.emit(1)
        self.download_finished.emit()
        return

    def _on_upload_file_progress_wrapper(
        self, reply: QNetworkReply, filename: str
    ) -> Callable:
        def cb(bytes_sent: int, bytes_total: int) -> None:
            self._on_upload_file_progress(reply, bytes_sent, bytes_total, filename)

        return cb

    def _on_upload_file_progress(
        self, reply: QNetworkReply, bytes_sent: int, bytes_total: int, filename: str
    ) -> None:
        # there are always at least a few bytes to send, so ignore this situation
        if bytes_total == 0:
            return

        self._files_to_upload[filename]["bytes_transferred"] = bytes_sent
        self._files_to_upload[filename]["bytes_total"] = bytes_total

        bytes_sent_sum = sum(
            [
                self._files_to_upload[filename]["bytes_transferred"]
                for filename in self._files_to_upload
            ]
        )
        bytes_total_sum = max(
            sum(
                [
                    self._files_to_upload[filename]["bytes_total"]
                    for filename in self._files_to_upload
                ]
            ),
            self.upload_bytes_total_files_only,
        )

        fraction = (
            min(bytes_sent_sum / bytes_total_sum, 1)
            if self.upload_bytes_total_files_only > 0
            else 1
        )

        self.upload_progress.emit(fraction)

    def _on_upload_file_finished_wrapper(
        self, reply: QNetworkReply, filename: str
    ) -> Callable:
        def cb() -> None:
            self._on_upload_file_finished(reply, filename)

        return cb

    def _on_upload_file_finished(self, reply: QNetworkReply, filename: str) -> None:
        try:
            self.network_manager.handle_response(reply, False)
        except Exception as err:
            self.error.emit(self.tr(f'Uploading file "{filename}" failed: {err}'), err)
            self.abort_requests()
            return

        self.upload_files_finished += 1

        if self.upload_files_finished == len(self._files_to_upload):
            self.upload_progress.emit(1)
            self.upload_finished.emit()
            # NOTE check _on_upload_finished
            return

    def _on_delete_file_finished_wrapper(
        self, reply: QNetworkReply, filename: str
    ) -> Callable:
        def cb() -> None:
            self._on_delete_file_finished(reply, filename)

        return cb

    def _on_delete_file_finished(self, reply: QNetworkReply, filename: str) -> None:
        self.delete_files_finished += 1

        try:
            self.network_manager.handle_response(reply, False)
        except Exception as err:
            self.error.emit(self.tr(f'Deleting file "{filename}" failed.'), err)

        if self.delete_files_finished == len(self._files_to_delete):
            self.delete_finished.emit()

    def _on_upload_finished(self) -> None:
        self.is_upload_active = False
        self._update_project_files_list()
        self._delete()

    def _on_delete_finished(self) -> None:
        self.is_delete_active = False
        self._download()

    def _on_download_finished(self) -> None:
        self.is_download_active = False
        self.is_finished = True
        self.import_qfield_project()

        if not self.is_project_list_update_active:
            self.finished.emit()

    def _update_project_files_list(self) -> None:
        self.is_project_list_update_active = True

        reply = self.network_manager.projects_cache.get_project_files(
            self.cloud_project.id
        )
        reply.finished.connect(lambda: self._on_update_project_files_list_finished())

    def _on_update_project_files_list_finished(self) -> None:
        self.is_project_list_update_active = False

        if not self.is_download_active:
            self.finished.emit()

    def abort_requests(self) -> None:
        if self.is_aborted:
            return

        self.is_aborted = True

        for reply in self.replies:
            if not reply.isFinished():
                reply.abort()

        if self.throttled_downloader:
            self.throttled_downloader.abort()

        self.abort.emit()

    def _make_backup(self) -> None:
        for project_file in [
            *list(map(lambda f: f["project_file"], self._files_to_upload.values())),
            *list(map(lambda f: f, self._files_to_download.values())),
        ]:
            if project_file.local_path and project_file.local_path.exists():
                dest = self.temp_dir.joinpath("backup", project_file.path)
                dest.parent.mkdir(parents=True, exist_ok=True)

                copy_multifile(project_file.local_path, dest)

    def _temp_dir2main_dir(self, subdir: str) -> None:
        subdir_path = self.temp_dir.joinpath(subdir)

        assert subdir_path.exists()

        for filename in subdir_path.glob("**/*"):
            if filename.is_dir():
                filename.mkdir(parents=True, exist_ok=True)
                continue

            source_filename = str(Path(filename).relative_to(subdir_path))
            dest_filename = str(self._files_to_download[source_filename].local_path)

            dest_path = Path(dest_filename)
            if not dest_path.parent.exists():
                dest_path.parent.mkdir(parents=True)

            if source_filename.endswith((".gpkg-shm", ".gpkg-wal")):
                for suffix in ("-shm", "-wal"):
                    source_path = Path(str(self.local_path) + suffix)
                    dest_path = Path(str(dest_filename) + suffix)

                    if source_path.exists():
                        shutil.copyfile(source_path, dest_path)
                    else:
                        dest_path.unlink()

            shutil.copyfile(filename, dest_filename)

    def import_qfield_project(self) -> None:
        try:
            self._temp_dir2main_dir(str(self.temp_dir.joinpath("download")))
        except Exception as err:
            self.error.emit(
                "Failed to copy downloaded files to your project. Trying to rollback changes...",
                err,
            )
            try:
                self._temp_dir2main_dir(str(self.temp_dir.joinpath("backup")))
            except Exception as errInner:
                self.error.emit(
                    'Failed to rollback the backup. You project might be corrupted! Please check ".qfieldsync/backup" directory and try to copy the files back manually.',
                    errInner,
                )

    def _on_logout_success(self) -> None:
        self.abort_requests()


class FileTransfer:
    def __init__(self, filename: str, destination: Path) -> None:
        self.replies: List[QNetworkReply] = []
        self.redirects: List[QUrl] = []
        self.filename = filename
        self.dest_filename = destination.joinpath(filename)
        self.dest_filename.parent.mkdir(parents=True, exist_ok=True)
        self.error: Optional[Exception] = None
        self.bytes_transferred = 0
        self.bytes_total = 0
        self.is_aborted = False

    @property
    def last_reply(self) -> QNetworkReply:
        if not self.replies:
            raise ValueError("There are no replies yet!")

        return self.replies[-1]

    @property
    def last_redirect_url(self) -> QUrl:
        if not self.replies:
            raise ValueError("There are no redirects!")

        return self.redirects[-1]

    @property
    def is_started(self) -> bool:
        return len(self.replies) > 0

    @property
    def is_finished(self) -> bool:
        if self.is_aborted:
            return True

        if not self.replies:
            return False

        if self.is_redirect:
            return False

        return self.replies[-1].isFinished()

    @property
    def is_redirect(self) -> bool:
        if not self.replies:
            return False

        return len(self.replies) == len(self.redirects)

    @property
    def is_failed(self) -> bool:
        if not self.replies:
            return False

        return self.last_reply.isFinished() and (
            self.error is not None or self.last_reply.error() != QNetworkReply.NoError
        )


class ThrottledFileTransferrer(QObject):
    error = pyqtSignal(str, str)
    finished = pyqtSignal()
    aborted = pyqtSignal()
    file_finished = pyqtSignal(str)
    progress = pyqtSignal(str, int, int)

    def __init__(
        self,
        network_manager,
        cloud_project,
        files: List[str],
        max_parallel_requests: int = 8,
    ) -> None:
        super(QObject, self).__init__()

        self.transfers: Dict[str, FileTransfer] = {}
        self.network_manager = network_manager
        self.cloud_project = cloud_project
        self.filenames = files
        self.max_parallel_requests = max_parallel_requests
        self.finished_count = 0
        self.temp_dir = Path(cloud_project.local_dir).joinpath(".qfieldsync")

    def download(self) -> None:
        for filename in self.filenames:
            self.transfers[filename] = FileTransfer(
                filename, self.temp_dir.joinpath("download")
            )

        self._download()

    def _download(self) -> None:
        transfers = []

        for transfer in self.transfers.values():
            if transfer.is_finished:
                continue

            transfers.append(transfer)

            if len(transfers) == self.max_parallel_requests:
                break

        for transfer in transfers:
            reply = None

            if transfer.is_redirect:
                reply = self.network_manager.get(
                    transfer.last_redirect_url, transfer.dest_filename
                )
            elif transfer.is_started:
                # started a request but still waiting for a response
                continue
            else:
                reply = self.network_manager.get_file_request(
                    self.cloud_project.id + "/" + transfer.filename + "/"
                )

            transfer.replies.append(reply)

            reply.redirected.connect(self._on_redirect_wrapper(transfer))
            reply.downloadProgress.connect(self._on_progress_wrapper(transfer))
            reply.finished.connect(self._on_finished_wrapper(transfer))

    def abort(self) -> None:
        for transfer in self.transfers.values():
            if not transfer.is_started:
                continue

            transfer.is_aborted = True
            transfer.last_reply.abort()

        self.aborted.emit()

    def _on_redirect_wrapper(self, transfer: FileTransfer) -> Callable:
        def on_redirected(url: QUrl) -> None:
            transfer.redirects.append(url)
            transfer.last_reply.abort()

            self._download()

        return on_redirected

    def _on_finished_wrapper(self, transfer: FileTransfer) -> Callable:
        def on_finished() -> None:
            # note if the redirect request failed, it will continue
            if transfer.is_redirect:
                return

            self._download()

            try:
                self.network_manager.handle_response(transfer.last_reply, False)
            except Exception as err:
                self.error.emit(
                    transfer.filename,
                    self.tr(
                        f'Downloaded file "{transfer.dest_filename}" had an HTTP error!'
                    ),
                )
                transfer.error = err
                return

            self.finished_count += 1

            if not Path(transfer.dest_filename).exists():
                self.error.emit(
                    transfer.filename,
                    self.tr(f'Downloaded file "{transfer.dest_filename}" not found!'),
                )
                return

            self.file_finished.emit(transfer.filename)

            if self.finished_count == len(self.transfers):
                self.finished.emit()
                return

        return on_finished

    def _on_progress_wrapper(self, transfer: FileTransfer) -> Callable:
        def on_progress(bytes_received: int, bytes_total: int):
            transfer.bytes_transferred = bytes_received
            transfer.bytes_total = bytes_total
            bytes_received_sum = sum(
                [t.bytes_transferred for t in self.transfers.values()]
            )
            bytes_total_sum = sum([t.bytes_total for t in self.transfers.values()])
            self.progress.emit(transfer.filename, bytes_received_sum, bytes_total_sum)

        return on_progress
