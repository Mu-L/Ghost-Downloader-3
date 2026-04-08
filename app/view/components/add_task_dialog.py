from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Self

from PySide6.QtCore import QEvent, Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QColor, QTextOption
from PySide6.QtWidgets import QDialog, QFileDialog, QVBoxLayout
from loguru import logger
from qfluentwidgets import (
    MessageBoxBase,
    SubtitleLabel,
    LineEdit,
    Action,
    FluentIcon,
    IndeterminateProgressBar,
    InfoBar,
    InfoBarPosition,
    Slider,
    BodyLabel,
    FluentTitleBar,
)
from qfluentwidgets.common.style_sheet import FluentStyleSheet
from qframelesswindow import FramelessDialog

from app.bases.models import Task
from app.services.core_service import coreService
from app.services.feature_service import featureService
from app.supports.config import DEFAULT_HEADERS, cfg
from app.supports.utils import getProxies
from app.view.components.card_widgets import (
    ParseResultHeaderCardWidget,
    ParseSettingHeaderCardWidget,
)
from app.view.components.cards import ParseSettingCard, ResultCard
from app.view.components.editors import AutoSizingEdit


class SelectFolderCard(ParseSettingCard):
    def initCustomWidget(self):
        # init widget
        self.pathEdit = LineEdit(self)
        self.selectFolderAction = Action(FluentIcon.FOLDER, self.tr("选择文件夹"), self)
        self.selectFolderAction.triggered.connect(self._selectFolder)
        self.pathEdit.addAction(self.selectFolderAction)
        self.pathEdit.setReadOnly(True)
        self.pathEdit.setText(cfg.downloadFolder.value)
        # init layout
        self.hBoxLayout.addWidget(self.pathEdit, stretch=3)

    def _selectFolder(self):
        path = Path(self.pathEdit.text())
        if path.exists():
            path = path.absolute()
        else:
            path = path.parent

        path = QFileDialog.getExistingDirectory(
            self, self.tr("选择下载路径"), str(path)
        )
        if path:
            self.pathEdit.setText(path)
            self.payloadChanged.emit()

    @property
    def payload(self) -> dict[str, Any]:
        return {"path": Path(self.pathEdit.text())}


class PreBlockNumCard(ParseSettingCard):
    def initCustomWidget(self):
        self.slider = Slider(Qt.Orientation.Horizontal, self)
        self.valueLabel = BodyLabel(self)
        self.slider.setMinimumWidth(268)

        self.slider.setSingleStep(1)
        self.slider.setRange(*cfg.preBlockNum.range)
        self.slider.setValue(cfg.preBlockNum.value)
        self.valueLabel.setNum(cfg.preBlockNum.value)

        self.hBoxLayout.addWidget(self.valueLabel)
        self.hBoxLayout.addSpacing(6)
        self.hBoxLayout.addWidget(self.slider)
        self.hBoxLayout.addSpacing(16)

        self.slider.valueChanged.connect(self._onValueChanged)

    def _onValueChanged(self, value: int):
        self.valueLabel.setNum(value)
        self.valueLabel.adjustSize()
        self.slider.setValue(value)
        self.payloadChanged.emit()

    @property
    def payload(self) -> dict[str, Any]:
        return {"preBlockNum": self.slider.value()}


class _StandaloneWrapper(FramelessDialog):
    """Independent window wrapper for AddTaskDialog standalone mode."""

    def __init__(self, addTaskDialog: "AddTaskDialog"):
        super().__init__()
        self._addTaskDialog = addTaskDialog

        titleBar = FluentTitleBar(self)
        titleBar.maxBtn.hide()
        titleBar.iconLabel.hide()
        titleBar.setDoubleClickEnabled(False)
        self.setTitleBar(titleBar)
        self.setWindowTitle(addTaskDialog.tr("添加任务"))

        self._contentLayout = QVBoxLayout(self)
        self._contentLayout.setContentsMargins(0, 48, 0, 0)
        self._contentLayout.setSpacing(0)

        FluentStyleSheet.DIALOG.apply(self)

    def setContent(self, widget):
        self._contentLayout.addWidget(widget)

    def takeContent(self, widget):
        self._contentLayout.removeWidget(widget)

    def closeEvent(self, event):
        event.ignore()
        self._addTaskDialog.reject()


@dataclass
class _LineParseState:
    url: str
    requestId: int = 0
    callbackId: str = ""
    status: str = "idle"
    task: Task | None = None
    resultCard: ResultCard | None = None


@dataclass
class _AcceptedPendingParse:
    payload: dict[str, Any]


class AddTaskDialog(MessageBoxBase):
    taskConfirmed = Signal(object)

    instance: Self = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(self.tr("添加任务"), self)
        self.urlEdit = AutoSizingEdit(self)
        self.parseProgressBar = IndeterminateProgressBar(self)
        self.parseResultGroup = ParseResultHeaderCardWidget(self)
        self.settingGroup = ParseSettingHeaderCardWidget(self)
        self.selectFolderCard = SelectFolderCard(FluentIcon.DOWNLOAD, self.tr('选择下载路径'), self)
        self.preBlockNumCard = PreBlockNumCard(FluentIcon.CLOUD, self.tr("预分配线程数"), self)

        self._timer = QTimer(self, singleShot=True)
        self._lineStates: list[_LineParseState] = []
        self._activeRequests: dict[int, _LineParseState] = {}
        self._acceptedPendingParses: dict[int, _AcceptedPendingParse] = {}
        self._confirmedTasks: list[Task] = []
        self._payloadOverrides: dict[str, dict[str, Any]] = {}  # TODO, 这是一种临时解决方案, 最佳方案是让 ResultCard 可以自定义 Payload
        self._requestSerial = 0

        self._maskParent = parent
        self._standaloneWrapper: _StandaloneWrapper | None = None
        self._isStandaloneMode = False
        self._isSwitchingMode = False

        self.initWidget()
        self.initLayout()
        self.connectSignalToSlot()

    def initWidget(self):
        self.setObjectName("AddTaskDialog")
        self.widget.setFixedWidth(700)

        self.urlEdit.setPlaceholderText(
            self.tr("添加多个下载链接时，请确保每行只有一个下载链接")
        )
        self.urlEdit.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.parseProgressBar.hide()

        self.settingGroup.addCard(self.selectFolderCard)
        self.settingGroup.addCard(self.preBlockNumCard)
        for card in featureService.getDialogCards(self.settingGroup):
            self.settingGroup.addCard(card)
            card.payloadChanged.connect(self.syncPayload)

    def initLayout(self):
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.urlEdit)
        self.viewLayout.addWidget(self.parseProgressBar)
        self.viewLayout.addWidget(self.parseResultGroup)
        self.viewLayout.addWidget(self.settingGroup)

    def connectSignalToSlot(self):
        self._timer.timeout.connect(self.parse)
        self.urlEdit.textChanged.connect(
            lambda: (self._timer.stop(), self._timer.start(1000))
        )
        self.selectFolderCard.payloadChanged.connect(self.syncPayload)
        self.preBlockNumCard.payloadChanged.connect(self.syncPayload)

    def parse(self):
        """按行同步解析输入的 URL 列表"""
        currentUrls = self._getEditorUrls()
        previousStates = self._lineStates
        previousUrls = [state.url for state in previousStates]
        nextStates: list[_LineParseState] = []
        matcher = SequenceMatcher(a=previousUrls, b=currentUrls, autojunk=False)

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                nextStates.extend(previousStates[i1:i2])
                continue

            for state in previousStates[i1:i2]:
                self._disposeLineState(state, cancelRequest=True)

            for url in currentUrls[j1:j2]:
                state = _LineParseState(url=url)
                self._submitParse(state)
                nextStates.append(state)

        self._lineStates = nextStates
        self._rebuildResultCards()

    def _getEditorUrls(self) -> list[str]:
        text = self.urlEdit.toPlainText()
        if not text:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    def appendUrls(self, urls: list[str]):
        if not urls:
            return

        existingUrls = set(self._getEditorUrls())
        appendableUrls: list[str] = []

        for url in urls:
            normalizedUrl = url.strip()
            if not normalizedUrl or normalizedUrl in existingUrls:
                continue
            existingUrls.add(normalizedUrl)
            appendableUrls.append(normalizedUrl)

        if not appendableUrls:
            return

        self.urlEdit.appendPlainText("\n".join(appendableUrls))
        self._timer.stop()
        self.parse()

    def appendUrlWithPayload(self, url: str, payloadOverride: dict[str, Any]):
        self._payloadOverrides[url] = payloadOverride
        self.appendUrls([url])

    def appendParsedTasks(self, tasks: list[Task]):
        """Add pre-parsed tasks directly, skipping URL re-parse."""
        if not tasks:
            return

        existingUrls = {state.url for state in self._lineStates}
        newUrlLines: list[str] = []

        for task in tasks:
            url = task.url
            if url in existingUrls:
                for state in self._lineStates:
                    if state.url == url:
                        if state.status == "success":
                            break
                        self._disposeLineState(state, cancelRequest=True)
                        self._applyParsedTaskToState(state, task)
                        break
                continue

            existingUrls.add(url)
            newUrlLines.append(url)
            state = _LineParseState(url=url)
            self._applyParsedTaskToState(state, task)
            self._lineStates.append(state)

        if newUrlLines:
            self.urlEdit.blockSignals(True)
            self.urlEdit.appendPlainText("\n".join(newUrlLines))
            self.urlEdit.blockSignals(False)

        self._rebuildResultCards()

    def _applyParsedTaskToState(self, state: _LineParseState, task: Task):
        try:
            state.task = task
            state.status = "success"
            state.resultCard = featureService.createResultCard(
                task, self.parseResultGroup
            )
        except Exception as e:
            state.status = "error"
            state.task = None
            logger.opt(exception=e).error("无法创建解析结果卡片 {}", state.url)
            self._showParseError(state.url, self.tr("解析结果处理失败"))

    def getPayload(self, url) -> dict[str, Any]:
        payload = self.getCurrentPayload()
        payload.update(self._payloadOverrides.get(url, {}))
        payload["url"] = url
        return payload

    def getCurrentPayload(self) -> dict[str, Any]:
        payload = {
            "headers": DEFAULT_HEADERS.copy(),
            "proxies": getProxies(),
        }
        payload.update(self.settingGroup.payload)
        return payload

    def _applyCurrentPayloadToTask(self, task: Task):
        payload = self.getCurrentPayload()
        task.applyPayloadToTask(payload)

    def syncPayload(self):
        for state in self._lineStates:
            if state.task is None:
                continue
            try:
                self._applyCurrentPayloadToTask(state.task)
            except Exception as e:
                logger.opt(exception=e).error("同步解析结果设置失败 {}", state.url)

    def _submitParse(self, state: _LineParseState):
        self._requestSerial += 1
        requestId = self._requestSerial

        state.requestId = requestId
        state.status = "pending"
        state.task = None
        self._activeRequests[requestId] = state
        self._refreshParsingState()

        try:
            state.callbackId = coreService.parseUrl(
                self.getPayload(state.url),
                lambda resultTask, error=None, requestId=requestId: self._handleParseResult(
                    requestId, resultTask, error
                ),
            )
        except Exception as e:
            self._activeRequests.pop(requestId, None)
            state.callbackId = ""
            state.status = "error"
            self._refreshParsingState()
            logger.opt(exception=e).error("提交解析请求失败 {}", state.url)
            self._showParseError(state.url, str(e))

    def _removeResultCard(self, state: _LineParseState):
        if state.resultCard is None:
            return

        self.parseResultGroup.scrollLayout.removeWidget(state.resultCard)
        self.parseResultGroup.updateGeometry()
        state.resultCard.deleteLater()
        state.resultCard = None

    def _disposeLineState(self, state: _LineParseState, cancelRequest: bool):
        if cancelRequest and state.requestId:
            self._activeRequests.pop(state.requestId, None)
            if state.callbackId:
                coreService.removeCallback(state.callbackId)
            self._refreshParsingState()

        self._payloadOverrides.pop(state.url, None)
        state.callbackId = ""
        self._removeResultCard(state)
        state.task = None
        state.status = "idle" if state.url else "empty"

    def _rebuildResultCards(self):
        visibleIndex = 0

        for state in self._lineStates:
            if state.resultCard is None:
                continue

            if self.parseResultGroup.scrollLayout.indexOf(state.resultCard) != visibleIndex:
                self.parseResultGroup.scrollLayout.insertWidget(
                    visibleIndex,
                    state.resultCard,
                    alignment=Qt.AlignmentFlag.AlignTop,
                )
            visibleIndex += 1

        self.parseResultGroup.updateGeometry()

    def _infoBarParent(self):
        if self._isStandaloneMode and self._standaloneWrapper is not None:
            return self._standaloneWrapper
        return self

    def _showParseError(self, url: str, error: str | None = None):
        displayUrl = url if len(url) <= 48 else f"{url[:45]}..."

        content = self.tr("{0}\n{1}").format(displayUrl, error)

        InfoBar.error(
            self.tr("链接解析失败"),
            content,
            duration=-1,
            position=InfoBarPosition.BOTTOM_RIGHT,
            parent=self._infoBarParent(),
        )

    def _refreshParsingState(self):
        self.parseProgressBar.setVisible(bool(self._activeRequests))

    def _handleParseResult(self, requestId: int, resultTask: Task, error: str = None):
        state = self._activeRequests.pop(requestId, None)
        if state is not None:
            self._refreshParsingState()
            state.callbackId = ""

            if error or resultTask is None:
                state.status = "error"
                state.task = None
                self._removeResultCard(state)
                self._showParseError(state.url, error or self.tr("解析失败"))
                if error:
                    logger.warning("解析任务失败 {}: {}", state.url, error)
                return

            try:
                self._applyCurrentPayloadToTask(resultTask)
                state.task = resultTask
                state.status = "success"
                if state.resultCard is None:
                    state.resultCard = featureService.createResultCard(
                        resultTask, self.parseResultGroup
                    )
                self._rebuildResultCards()
            except Exception as e:
                state.status = "error"
                state.task = None
                self._removeResultCard(state)
                logger.opt(exception=e).error("无法创建解析结果卡片 {}", state.url)
                self._showParseError(state.url, self.tr("解析结果处理失败"))
            return

        acceptedParse = self._acceptedPendingParses.pop(requestId, None)
        if acceptedParse is None:
            return

        if error or resultTask is None:
            if error:
                logger.warning("后台确认任务解析失败: {}", error)
            return

        try:
            resultTask.applyPayloadToTask(acceptedParse.payload)
            self.taskConfirmed.emit(resultTask)
        except Exception as e:
            logger.opt(exception=e).error("无法创建任务卡片 {}", getattr(resultTask, "title", "Unknown"))

    def _clearEditorState(self):
        self._timer.stop()
        for state in self._lineStates:
            self._disposeLineState(state, cancelRequest=True)
        self._lineStates.clear()
        self.parseResultGroup.clearResults()

        self.urlEdit.blockSignals(True)
        self.urlEdit.clear()
        self.urlEdit.blockSignals(False)

    def _commitAcceptedTasks(self):
        self._confirmedTasks.clear()
        acceptedPayload = self.getCurrentPayload()

        for state in self._lineStates:
            if state.status == "success" and state.task is not None:
                try:
                    state.task.applyPayloadToTask(acceptedPayload)
                    self._confirmedTasks.append(state.task)
                except Exception as e:
                    logger.opt(exception=e).error("同步已确认任务设置失败 {}", state.url)
            elif state.status == "pending" and state.requestId:
                self._activeRequests.pop(state.requestId, None)
                self._acceptedPendingParses[state.requestId] = _AcceptedPendingParse(
                    payload=acceptedPayload,
                )
                state.callbackId = ""

        self._refreshParsingState()

        self._timer.stop()
        for state in self._lineStates:
            keepPendingRequest = (
                state.status == "pending" and state.requestId in self._acceptedPendingParses
            )
            self._disposeLineState(state, cancelRequest=not keepPendingRequest)
        self._lineStates.clear()
        self.parseResultGroup.clearResults()

        self.urlEdit.blockSignals(True)
        self.urlEdit.clear()
        self.urlEdit.blockSignals(False)

    def takeConfirmedTasks(self) -> list[Task]:
        tasks = self._confirmedTasks.copy()
        self._confirmedTasks.clear()
        return tasks

    @property
    def isStandaloneMode(self) -> bool:
        return self._isStandaloneMode

    def _ensureStandaloneWrapper(self) -> _StandaloneWrapper:
        if self._standaloneWrapper is None:
            self._standaloneWrapper = _StandaloneWrapper(self)
        return self._standaloneWrapper

    def _enterStandaloneMode(self):
        """Move self.widget from mask overlay into the standalone wrapper."""
        wrapper = self._ensureStandaloneWrapper()
        if self._maskParent is not None:
            self._maskParent.removeEventFilter(self)
        self.setParent(None)
        self._hBoxLayout.removeWidget(self.widget)
        wrapper.setContent(self.widget)
        self.widget.setStyleSheet("#centerWidget { border: none; border-radius: 0; }")
        self.widget.show()
        self.titleLabel.hide()
        self._isStandaloneMode = True

    def _exitStandaloneMode(self):
        """Move self.widget back into the mask overlay."""
        if not self._isStandaloneMode:
            return
        wrapper = self._standaloneWrapper
        if wrapper is not None:
            wrapper.hide()
            wrapper.takeContent(self.widget)
        self.widget.setStyleSheet("")
        self._hBoxLayout.addWidget(self.widget, 1, Qt.AlignCenter)
        self.widget.show()
        self.titleLabel.show()
        self.setParent(self._maskParent)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        if self._maskParent is not None:
            self._maskParent.installEventFilter(self)
        self._isStandaloneMode = False

    def showStandalone(self):
        """Show dialog as an independent standalone window."""
        if self._isStandaloneMode and self._standaloneWrapper is not None and self._standaloneWrapper.isVisible():
            self._standaloneWrapper.raise_()
            self._standaloneWrapper.activateWindow()
            return

        if self.isVisible() and not self._isStandaloneMode:
            self._isSwitchingMode = True
            self.setGraphicsEffect(None)
            self.widget.setGraphicsEffect(None)
            QDialog.done(self, QDialog.DialogCode.Rejected)
            self._isSwitchingMode = False

        if not self._isStandaloneMode:
            self._enterStandaloneMode()

        wrapper = self._standaloneWrapper
        wrapper.resize(self.widget.width(), 600)
        wrapper.show()
        wrapper.raise_()
        wrapper.activateWindow()

    def showMask(self) -> int:
        """Show dialog as a mask overlay (blocks via exec).
        Returns the QDialog result code."""
        if self._isStandaloneMode:
            self._exitStandaloneMode()

        # MaskDialogBase.done() clears widget.graphicsEffect via setGraphicsEffect(None),
        # so shadow and mask must be re-applied before each exec().
        if self._maskParent is not None:
            self.setGeometry(0, 0, self._maskParent.width(), self._maskParent.height())
            self.windowMask.resize(self.size())
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 50))
        self.setMaskColor(QColor(0, 0, 0, 76))

        return self.exec()

    def done(self, code):
        if self._isSwitchingMode:
            QDialog.done(self, code)
            return

        if code == QDialog.DialogCode.Rejected:
            self._confirmedTasks.clear()
            self._clearEditorState()
        elif code == QDialog.DialogCode.Accepted:
            self._commitAcceptedTasks()

        for task in self.takeConfirmedTasks():
            self.taskConfirmed.emit(task)

        if self._isStandaloneMode:
            self._exitStandaloneMode()
        else:
            super().done(code)

    def validate(self) -> bool:
        self._timer.stop()
        self.parse()

        return any(state.status in {"pending", "success"} for state in self._lineStates)

    @classmethod
    def initialize(cls, mainWindow) -> Self:
        """Create the singleton (if needed) and connect taskConfirmed to mainWindow.addTask."""
        if cls.instance is None:
            cls.instance = cls(mainWindow)
            cls.instance.taskConfirmed.connect(mainWindow.addTask)
        return cls.instance

    def eventFilter(self, obj, e: QEvent):
        if obj is self.windowMask:
            if (
                e.type() == QEvent.Type.MouseButtonPress
                and e.button() == Qt.MouseButton.LeftButton
            ):
                self._dragPos = e.pos()
                return True
            elif e.type() == QEvent.Type.MouseMove and not self._dragPos.isNull():
                window = self.window()
                if window.isMaximized():
                    window.showNormal()

                pos = window.pos() + e.pos() - self._dragPos
                pos.setX(max(0, pos.x()))
                pos.setY(max(0, pos.y()))

                window.move(pos)
                return True
            elif e.type() == QEvent.Type.MouseButtonRelease:
                self._dragPos = QPoint()

        return super().eventFilter(obj, e)
