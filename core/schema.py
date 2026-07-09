from utils import fastjson


class LogInput:
    def __init__(self, kafka_value):
        try:
            # fastjson.loads handles both bytes (straight off Kafka) and str.
            data = fastjson.loads(kafka_value)
            self.meta = data.get("meta", {})
            raw = data.get("raw", "")
            self.raw = raw.strip() if isinstance(raw, str) else str(raw).strip()
            self.program = self.meta.get("source_program", "unknown")
            self.valid = True
        except Exception:
            self.valid = False
            try:
                if isinstance(kafka_value, (bytes, bytearray)):
                    self.raw = kafka_value.decode("utf-8", "ignore")
                else:
                    self.raw = str(kafka_value)
            except Exception:
                self.raw = ""
            self.program = "unknown"
            self.meta = {}
