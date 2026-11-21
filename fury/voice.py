"""Voice Foundation layer for FURY as defined in Blueprint Phase 1.
Brokers WebRTC voice sessions with OpenAI GPT-Live-1 / Realtime API,
keeping API keys strictly server-side and enforcing session governance.
Supports offline duplex voice simulation / Web Speech fallback when unconfigured."""
import json, os, time, uuid, urllib.request, urllib.error
from typing import Any, Dict, Optional
from . import telemetry

VOICE_SYSTEM_PROMPT = """You are FURY, an intelligent operational voice companion for SREs.
- Assist engineers by investigating infrastructure incidents, checking statuses, and explaining runbooks.
- You have NO direct execution authority on the infrastructure. All investigations and actions are brokered by the FURY backend.
- Be concise, professional, evidence-focused, and action-oriented.
- Distinguish observed facts from hypotheses."""

class VoiceSessionBroker:
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-realtime-preview"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("FURY_VOICE_MODEL", model)

    def get_capabilities(self) -> Dict[str, Any]:
        has_key = bool(self.api_key and self.api_key.startswith("sk-"))
        return {
            "webrtc_available": True,
            "provider": "openai_gpt_live" if has_key else "simulated_local",
            "model": self.model if has_key else "local_voice_duplex",
            "full_duplex": True,
            "interruptions_supported": True,
            "server_side_brokered": True,
            "authenticated": True,
            "offline_mode": not has_key
        }

    def create_session(self, user_id: str = "engineer", session_id: Optional[str] = None) -> Dict[str, Any]:
        sid = session_id or f"voice-{uuid.uuid4().hex[:12]}"
        has_key = bool(self.api_key and self.api_key.startswith("sk-"))

        with telemetry.span("voice.session.create", user_id=user_id, session_id=sid, mode="live" if has_key else "simulated") as sp:
            telemetry.counter("voice.session.started", mode="live" if has_key else "simulated")

            if has_key:
                try:
                    payload = {
                        "model": self.model,
                        "voice": "alloy",
                        "instructions": VOICE_SYSTEM_PROMPT,
                        "modalities": ["audio", "text"],
                        "temperature": 0.6,
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500
                        }
                    }
                    req = urllib.request.Request(
                        "https://api.openai.com/v1/realtime/sessions",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json"
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        ephemeral_token = data.get("client_secret", {}).get("value")
                        telemetry.set_attr(sp, "ephemeral_issued", bool(ephemeral_token))
                        return {
                            "session_id": sid,
                            "mode": "webrtc_live",
                            "model": self.model,
                            "client_secret": ephemeral_token,
                            "webrtc_url": "https://api.openai.com/v1/realtime",
                            "expires_at": data.get("client_secret", {}).get("expires_at"),
                            "turn_detection": "server_vad"
                        }
                except Exception as e:
                    telemetry.set_attr(sp, "error", str(e)[:120])
                    telemetry.counter("voice.session.error", reason=type(e).__name__)
                    return {
                        "session_id": sid,
                        "mode": "simulated_local",
                        "model": "fury-duplex-offline",
                        "notice": f"OpenAI Realtime session broker notice: {e}. Falling back to browser duplex voice channel.",
                        "webrtc_available": True
                    }
            else:
                return {
                    "session_id": sid,
                    "mode": "simulated_local",
                    "model": "fury-duplex-offline",
                    "notice": "Running in local duplex voice simulation mode. Voice input is transcribed and processed through FURY backend.",
                    "webrtc_available": True
                }

    def record_turn(self, session_id: str, user_text: str, agent_text: str,
                    latency_ms: float = 0.0, interrupted: bool = False):
        with telemetry.span("voice.turn", session_id=session_id, latency_ms=latency_ms, interrupted=interrupted) as sp:
            telemetry.histogram("voice.turn.latency_ms", latency_ms)
            if interrupted:
                telemetry.counter("voice.turn.interrupted")
            telemetry.counter("voice.turn.completed")

    def end_session(self, session_id: str, interruptions: int = 0, latency_first_audio_ms: float = 0.0):
        with telemetry.span("voice.session.end", session_id=session_id) as sp:
            telemetry.counter("voice.session.completed")
            telemetry.histogram("voice.session.latency_first_audio_ms", latency_first_audio_ms)
