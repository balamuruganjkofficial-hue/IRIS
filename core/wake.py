from pathlib import Path

from core.config import CFG, ROOT

_model = None


def _load():
    global _model
    if _model is None:
        import openwakeword
        from openwakeword.model import Model
        name = str(CFG["voice"].get("wake_word", "hey_jarvis"))
        if name.lower().endswith(".onnx"):          # a custom trained model file
            p = Path(name)
            name = str(p if p.is_absolute() else ROOT / p)
        try:
            _model = Model(wakeword_models=[name], inference_framework="onnx")
        except Exception:
            openwakeword.utils.download_models()
            _model = Model(wakeword_models=[name], inference_framework="onnx")
    return _model
