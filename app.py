import json
import os
import queue
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

try:
    import sounddevice as sd
    from vosk import KaldiRecognizer, Model

    VOICE_LIBS_AVAILABLE = True
except ImportError:
    VOICE_LIBS_AVAILABLE = False

QUESTION_BANK = [
    {"key": "name", "text": "您好，请问您的姓名是？"},
    {"key": "gender", "text": "您的性别是？"},
    {"key": "age", "text": "您的年龄是？"},
    {"key": "chiefComplaint", "text": "您这次最主要的不舒服是什么？"},
    {"key": "onsetTime", "text": "这个症状大概从什么时候开始的？"},
    {"key": "symptomDetail", "text": "症状具体是怎样的？是否持续或间断？"},
    {"key": "history", "text": "以前有类似情况或慢性病史吗？"},
    {"key": "allergy", "text": "您是否有药物或食物过敏史？"},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_structured_case_text(session: dict) -> str:
    answer_map = {item["question_key"]: item["answer_text"] for item in session["answers"]}
    return (
        f"结构化病例：患者{answer_map.get('name', '（姓名未提供）')}，"
        f"{answer_map.get('gender', '性别未提供')}，"
        f"{answer_map.get('age', '年龄未提供')}岁；"
        f"主诉“{answer_map.get('chiefComplaint', '未提供')}”；"
        f"起病时间为{answer_map.get('onsetTime', '未提供')}；"
        f"症状表现为{answer_map.get('symptomDetail', '未提供')}；"
        f"既往史：{answer_map.get('history', '未提供')}；"
        f"过敏史：{answer_map.get('allergy', '未提供')}。"
    )


class VoiceRecognizerWorker(QObject):
    final_text = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, model_path: str, sample_rate: int = 16000):
        super().__init__()
        self.model_path = model_path
        self.sample_rate = sample_rate
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        if not VOICE_LIBS_AVAILABLE:
            self.error.emit("未安装语音识别依赖，请安装 requirements.txt。")
            self.finished.emit()
            return

        audio_queue: queue.Queue[bytes] = queue.Queue()

        try:
            model = Model(self.model_path)
            recognizer = KaldiRecognizer(model, self.sample_rate)

            def callback(indata, frames, time_data, status):
                del frames, time_data
                if status:
                    self.error.emit(f"麦克风输入异常：{status}")
                audio_queue.put(bytes(indata))

            with sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=8000,
                dtype="int16",
                channels=1,
                callback=callback,
            ):
                while self._running:
                    try:
                        data = audio_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    recognizer.AcceptWaveform(data)

                final_result = json.loads(recognizer.FinalResult() or "{}")
                text = final_result.get("text", "").strip()
                self.final_text.emit(text)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"语音识别失败：{exc}")
        finally:
            self.finished.emit()


class IntakeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("问诊信息整理机器人（QL版）")
        self.resize(840, 700)

        self.session = None
        self.current_index = -1
        self.finished = False
        self.pending_input_mode = "text"
        self.voice_thread = None
        self.voice_worker = None
        self.is_listening = False
        self.voice_model_path = os.getenv(
            "VOSK_MODEL_PATH",
            str(Path(__file__).resolve().parent / "models" / "vosk-model-small-cn-0.22"),
        )

        self._build_ui()
        self.add_message("bot", "您好，我是问诊信息整理助手。点击“开始问诊”后，我会按顺序提问。")
        self._configure_voice_availability()

    def _build_ui(self):
        central = QWidget()
        central.setStyleSheet("background-color: #efeae2;")
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        header = QFrame()
        header.setStyleSheet("background-color: #075e54; border-radius: 8px;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)

        title = QLabel("问诊信息整理机器人")
        title.setStyleSheet("color: white;")
        title.setFont(QFont("Arial", 13, QFont.Bold))
        header_layout.addWidget(title)

        self.status_label = QLabel("状态：待开始")
        self.status_label.setStyleSheet("color: #d1fae5;")
        header_layout.addWidget(self.status_label, alignment=Qt.AlignRight)
        root_layout.addWidget(header)

        toolbar = QHBoxLayout()
        self.start_btn = QPushButton("开始问诊")
        self.start_btn.setStyleSheet(
            "QPushButton { background: #25d366; border: none; padding: 8px 14px; border-radius: 8px; }"
            "QPushButton:hover { background: #22c35e; }"
        )
        self.start_btn.clicked.connect(self.start_session)
        toolbar.addWidget(self.start_btn)
        toolbar.addStretch()
        root_layout.addLayout(toolbar)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("background-color: #e5ddd5; border: none; border-radius: 8px;")

        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(10, 10, 10, 10)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch()
        self.scroll_area.setWidget(self.chat_container)
        root_layout.addWidget(self.scroll_area, stretch=1)

        input_bar = QHBoxLayout()
        self.answer_input = QLineEdit()
        self.answer_input.setPlaceholderText("请输入患者回答...")
        self.answer_input.setEnabled(False)
        self.answer_input.returnPressed.connect(self.submit_answer)
        self.answer_input.textEdited.connect(self._on_text_edited)
        self.answer_input.setStyleSheet(
            "QLineEdit { background: white; border: 1px solid #d1d5db; border-radius: 8px; padding: 10px; }"
        )
        input_bar.addWidget(self.answer_input, stretch=1)

        self.voice_btn = QPushButton("语音输入")
        self.voice_btn.setEnabled(False)
        self.voice_btn.clicked.connect(self.toggle_voice_input)
        self.voice_btn.setStyleSheet(
            "QPushButton { background: #34b7f1; color: white; border: none; padding: 10px 14px; border-radius: 8px; }"
            "QPushButton:disabled { background: #90a4ae; }"
        )
        input_bar.addWidget(self.voice_btn)

        self.send_btn = QPushButton("发送")
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self.submit_answer)
        self.send_btn.setStyleSheet(
            "QPushButton { background: #128c7e; color: white; border: none; padding: 10px 16px; border-radius: 8px; }"
            "QPushButton:disabled { background: #90a4ae; }"
        )
        input_bar.addWidget(self.send_btn)
        root_layout.addLayout(input_bar)

    def add_message(self, sender: str, message: str):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        bubble = QFrame()
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(10, 8, 10, 8)
        bubble_layout.setSpacing(4)

        name = QLabel("机器人" if sender == "bot" else "患者")
        name.setStyleSheet("color: #6b7280; font-size: 12px; font-weight: bold;")
        bubble_layout.addWidget(name)

        content = QLabel(message)
        content.setWordWrap(True)
        content.setMaximumWidth(560)
        content.setStyleSheet("color: #111827; font-size: 14px;")
        bubble_layout.addWidget(content)

        if sender == "bot":
            bubble.setStyleSheet("background: white; border-radius: 10px;")
            row_layout.addWidget(bubble, alignment=Qt.AlignLeft)
            row_layout.addStretch()
        else:
            bubble.setStyleSheet("background: #dcf8c6; border-radius: 10px;")
            row_layout.addStretch()
            row_layout.addWidget(bubble, alignment=Qt.AlignRight)

        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _configure_voice_availability(self):
        if not VOICE_LIBS_AVAILABLE:
            self.voice_btn.setToolTip("请先安装语音依赖：pip install -r requirements.txt")
            self.add_message("bot", "提示：未检测到语音识别依赖，当前仅可文本输入。")
            return

        if not Path(self.voice_model_path).exists():
            self.voice_btn.setToolTip(f"请下载中文模型并放到：{self.voice_model_path}")
            self.add_message(
                "bot",
                "提示：未找到中文语音模型，当前仅可文本输入。下载模型后可启用语音输入。",
            )
            return

        self.voice_btn.setToolTip("点击开始语音，再次点击停止并识别")

    def _on_text_edited(self, _text: str):
        if not self.is_listening:
            self.pending_input_mode = "text"

    def start_session(self):
        self.stop_voice_input()
        self.session = {
            "id": str(uuid4()),
            "current_question_index": 0,
            "answers": [],
            "started_at": now_iso(),
        }
        self.current_index = 0
        self.finished = False
        self.status_label.setText("状态：问诊进行中")
        for i in reversed(range(self.chat_layout.count() - 1)):
            item = self.chat_layout.itemAt(i).widget()
            if item is not None:
                item.deleteLater()
        self.add_message("bot", "问诊开始。")
        self.add_message("bot", QUESTION_BANK[0]["text"])
        self.set_answer_input_state(True)

    def set_answer_input_state(self, enabled: bool):
        self.answer_input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        can_voice = enabled and VOICE_LIBS_AVAILABLE and Path(self.voice_model_path).exists()
        self.voice_btn.setEnabled(can_voice)
        if enabled:
            self.answer_input.clear()
            self.answer_input.setFocus()
            self.pending_input_mode = "text"

    def toggle_voice_input(self):
        if self.is_listening:
            self.stop_voice_input()
        else:
            self.start_voice_input()

    def start_voice_input(self):
        if self.session is None or self.finished:
            QMessageBox.warning(self, "提示", "请先点击开始问诊。")
            return

        if not VOICE_LIBS_AVAILABLE:
            QMessageBox.warning(self, "提示", "未安装语音识别依赖，请先安装 requirements.txt。")
            return

        if not Path(self.voice_model_path).exists():
            QMessageBox.warning(
                self,
                "提示",
                "未找到中文语音模型，请先下载模型并放到 models/vosk-model-small-cn-0.22，或设置环境变量 VOSK_MODEL_PATH。",
            )
            return

        self.is_listening = True
        self.status_label.setText("状态：语音识别中...")
        self.voice_btn.setText("停止语音")
        self.voice_btn.setStyleSheet(
            "QPushButton { background: #ef4444; color: white; border: none; padding: 10px 14px; border-radius: 8px; }"
            "QPushButton:disabled { background: #90a4ae; }"
        )

        self.voice_thread = QThread()
        self.voice_worker = VoiceRecognizerWorker(self.voice_model_path)
        self.voice_worker.moveToThread(self.voice_thread)

        self.voice_thread.started.connect(self.voice_worker.run)
        self.voice_worker.final_text.connect(self._on_voice_final_text)
        self.voice_worker.error.connect(self._on_voice_error)
        self.voice_worker.finished.connect(self._on_voice_finished)
        self.voice_worker.finished.connect(self.voice_thread.quit)
        self.voice_worker.finished.connect(self.voice_worker.deleteLater)
        self.voice_thread.finished.connect(self.voice_thread.deleteLater)
        self.voice_thread.start()

    def stop_voice_input(self):
        if self.voice_worker is not None:
            self.voice_worker.stop()

    def _on_voice_final_text(self, text: str):
        if not text:
            self.add_message("bot", "未识别到有效语音，请重试或直接文本输入。")
            return

        self.answer_input.setText(text)
        self.pending_input_mode = "voice"
        self.status_label.setText("状态：语音已识别，可发送")

    def _on_voice_error(self, message: str):
        QMessageBox.warning(self, "语音识别提示", message)
        self.status_label.setText("状态：语音识别失败")

    def _on_voice_finished(self):
        self.is_listening = False
        self.voice_worker = None
        self.voice_thread = None
        self.voice_btn.setText("语音输入")
        self.voice_btn.setStyleSheet(
            "QPushButton { background: #34b7f1; color: white; border: none; padding: 10px 14px; border-radius: 8px; }"
            "QPushButton:disabled { background: #90a4ae; }"
        )
        if self.finished:
            self.status_label.setText("状态：问诊结束")
        elif self.session is not None:
            if self.pending_input_mode == "voice" and self.answer_input.text().strip():
                self.status_label.setText("状态：语音已识别，可发送")
            else:
                self.status_label.setText("状态：问诊进行中")

    def submit_answer(self):
        if self.session is None or self.current_index < 0 or self.finished:
            QMessageBox.warning(self, "提示", "请先点击开始问诊。")
            return

        answer = self.answer_input.text().strip()
        if not answer:
            QMessageBox.warning(self, "提示", "回答不能为空。")
            return

        question = QUESTION_BANK[self.current_index]
        self.add_message("patient", answer)
        self.session["answers"].append(
            {
                "question_key": question["key"],
                "question_text": question["text"],
                "answer_text": answer,
                "input_mode": self.pending_input_mode,
                "answered_at": now_iso(),
            }
        )
        self.answer_input.clear()
        self.pending_input_mode = "text"

        self.current_index += 1
        self.session["current_question_index"] = self.current_index

        if self.current_index >= len(QUESTION_BANK):
            self.finish_session()
            return

        self.add_message("bot", QUESTION_BANK[self.current_index]["text"])

    def finish_session(self):
        self.stop_voice_input()
        self.finished = True
        self.status_label.setText("状态：问诊结束")
        self.set_answer_input_state(False)

        structured_case_text = build_structured_case_text(self.session)
        self.add_message(
            "bot",
            "问诊结束，已为您整理结构化病例。\n" + structured_case_text,
        )

    def closeEvent(self, event):
        self.stop_voice_input()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = IntakeWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
