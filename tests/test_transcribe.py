"""Tests for turning voice messages into text through the Whisper add-on."""

import base64
import json
import shutil
import socketserver
import subprocess
import threading
import time

import pytest

import invite_service as service


class FakeWhisper(socketserver.StreamRequestHandler):
    """Speaks just enough Wyoming: reads the sound, answers with a transcript."""

    seen = {}

    def handle(self):
        samples = 0
        while True:
            line = self.rfile.readline()
            if not line:
                return
            event = json.loads(line)
            if event.get("payload_length"):
                samples += len(self.rfile.read(event["payload_length"]))
            if event["type"] == "transcribe":
                FakeWhisper.seen["language"] = event["data"].get("language")
            if event["type"] == "audio-start":
                FakeWhisper.seen["rate"] = event["data"]["rate"]
            if event["type"] == "audio-stop":
                FakeWhisper.seen["bytes"] = samples
                answer = {"type": "transcript", "data": {"text": " Zdravo, kako si? "}}
                self.wfile.write(json.dumps(answer).encode() + b"\n")
                return


class Handler:
    pass


@pytest.fixture
def whisper(monkeypatch):
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeWhisper)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(service, "WHISPER_HOST", "127.0.0.1")
    monkeypatch.setattr(service, "WHISPER_PORT", server.server_address[1])
    monkeypatch.setattr(service, "caller", lambda handler: (handler.user, "t"))
    FakeWhisper.seen = {}
    yield
    server.shutdown()
    server.server_close()


def as_user(user):
    handler = Handler()
    handler.user = user
    return handler


def wait_for(user, job_id):
    for _ in range(200):
        answer = service.poll_transcribe(as_user(user), {}, job_id)
        if answer["status"] in ("done", "failed"):
            return answer
        time.sleep(0.05)
    raise AssertionError("never finished")


def test_voice_message_becomes_text(whisper, monkeypatch):
    monkeypatch.setattr(service, "to_pcm", lambda audio: b"\0\0" * 16000)
    job = service.start_transcribe(
        as_user("@ana:chat.test"), {"audio": base64.b64encode(b"sound").decode()}
    )
    answer = wait_for("@ana:chat.test", job["id"])
    assert answer["status"] == "done"
    assert answer["text"] == "Zdravo, kako si?"
    assert FakeWhisper.seen == {"language": "sr", "rate": 16000, "bytes": 32000}
    # The server forgets the text once the phone has it.
    with pytest.raises(service.ApiError):
        service.poll_transcribe(as_user("@ana:chat.test"), {}, job["id"])


def test_only_the_sender_can_read_the_text(whisper, monkeypatch):
    monkeypatch.setattr(service, "to_pcm", lambda audio: b"\0\0")
    job = service.start_transcribe(
        as_user("@ana:chat.test"), {"audio": base64.b64encode(b"x").decode(), "language": "auto"}
    )
    with pytest.raises(service.ApiError) as error:
        service.poll_transcribe(as_user("@bob:chat.test"), {}, job["id"])
    assert error.value.status == 404
    assert wait_for("@ana:chat.test", job["id"])["status"] == "done"
    assert FakeWhisper.seen["language"] is None


def test_missing_whisper_is_reported(monkeypatch):
    monkeypatch.setattr(service, "caller", lambda handler: (handler.user, "t"))
    monkeypatch.setattr(service, "WHISPER_HOST", "127.0.0.1")
    monkeypatch.setattr(service, "WHISPER_PORT", 1)
    monkeypatch.setattr(service, "to_pcm", lambda audio: b"\0\0")
    job = service.start_transcribe(as_user("@ana:chat.test"), {"audio": "eA=="})
    answer = wait_for("@ana:chat.test", job["id"])
    assert answer["status"] == "failed"
    assert answer["errcode"] == "DARTCHAT_NO_WHISPER"


def test_bad_requests_are_refused(monkeypatch):
    monkeypatch.setattr(service, "caller", lambda handler: (handler.user, "t"))
    for body in ({}, {"audio": "not base64!"}, {"audio": "eA==", "language": "Serbian"}):
        with pytest.raises(service.ApiError) as error:
            service.start_transcribe(as_user("@ana:chat.test"), body)
        assert error.value.status == 400


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_ffmpeg_turns_m4a_into_16khz_mono(tmp_path):
    source = tmp_path / "voice.m4a"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ar", "44100", "-ac", "2", "-c:a", "aac", str(source)],
        check=True,
    )
    pcm = service.to_pcm(source.read_bytes())
    assert abs(len(pcm) - 32000) < 3000
    with pytest.raises(service.TranscribeError):
        service.to_pcm(b"not a sound")
