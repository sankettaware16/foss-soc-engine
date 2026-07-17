import os
import yaml
import time
import logging
from threading import Thread
from .engine import UniversalEngine

logger = logging.getLogger("soc-engine")

class RuleRegistry:
    def __init__(self, rules_dir, program_map, include_original=True,
                 config_path=None):
        self.rules_dir = rules_dir
        self.program_map = program_map
        self.include_original = include_original
        # Optional: when set, the watcher also hot-reloads program_mapping
        # from this config.yaml (ONLY the mapping — kafka/batch/... still
        # need a restart).
        self.config_path = config_path
        self.engines = {}
        self._file_names = {}   # rule filename -> pattern_name (last good)
        self.reload()

        # Start Watcher
        self.watcher = Thread(target=self._watch_loop, daemon=True)
        self.watcher.start()

    def reload(self):
        """(Re)build every engine. FAIL-SAFE (audit P1-9): a broken edit —
        unparseable YAML, invalid regex/vars — keeps the PREVIOUS working
        version of that rule loaded instead of unloading it fleet-wide."""
        new_engines = {}
        new_file_names = {}
        old_engines = self.engines

        if not os.path.exists(self.rules_dir):
            logger.error(f"Rules directory not found: {self.rules_dir}")
            return

        for f in sorted(os.listdir(self.rules_dir)):
            if not f.endswith(".yaml"):
                continue
            try:
                with open(os.path.join(self.rules_dir, f), 'r') as yml:
                    rule_config = yaml.safe_load(yml)
                if not isinstance(rule_config, dict):
                    raise ValueError("rule file is not a YAML mapping")

                # Fallback: if 'pattern_name' missing, use filename
                pattern_name = rule_config.get('pattern_name', f.replace('.yaml', ''))
                eng = UniversalEngine(
                    rule_config,
                    include_original=self.include_original,
                )
                prev = old_engines.get(pattern_name)
                if eng.disabled and prev is not None and not prev.disabled:
                    logger.error(
                        f"Rule {f}: new version is INVALID - keeping the "
                        "previous working version (fix the file and save again)")
                    eng = prev
                new_engines[pattern_name] = eng
                new_file_names[f] = pattern_name

            except Exception as e:
                logger.error(f"Error loading rule {f}: {e}")
                prev_name = self._file_names.get(f)
                if prev_name and prev_name in old_engines:
                    logger.error(
                        f"Rule {f}: keeping the previous working version")
                    new_engines[prev_name] = old_engines[prev_name]
                    new_file_names[f] = prev_name

        self.engines = new_engines
        self._file_names = new_file_names
        logger.info(f"Loaded {len(self.engines)} parsing rules.")

    def reload_program_map(self):
        """Hot-reload ONLY program_mapping from config.yaml. A broken edit
        keeps the previous mapping (fail-safe, like rules)."""
        if not self.config_path:
            return
        try:
            with open(self.config_path, 'r') as f:
                cfg = yaml.safe_load(f) or {}
            pm = cfg.get('program_mapping') or {}
            if not isinstance(pm, dict):
                raise ValueError("program_mapping must be a mapping")
            if pm != self.program_map:
                self.program_map = pm
                logger.info(f"program_mapping hot-reloaded ({len(pm)} entries)")
        except Exception as e:
            logger.error(
                "config.yaml program_mapping reload failed - keeping the "
                f"previous mapping: {e}")

    def get_processors(self, source_program):
        """Resolve a source to its rule CHAIN: a list of (pattern_name, engine).

        program_mapping values may be a single rule name (classic) or a LIST
        of rule names — the engine tries them in order and the first rule
        that handles a line wins. This is how one messy source (a server tag
        that emits web logs AND app logs AND errors) uses several rule files.
        Falls back to a rule named exactly like the source program.
        Unknown rule names in a chain are skipped (test_config errors on them
        at deploy time)."""
        mapped = self.program_map.get(source_program)
        if mapped is None:
            names = [source_program]
        elif isinstance(mapped, (list, tuple)):
            names = [str(n) for n in mapped]
        else:
            names = [mapped]
        out = []
        for n in names:
            eng = self.engines.get(n)
            if eng is not None:
                out.append((n, eng))
        return out

    def get_processor(self, source_program):
        # Back-compat single-rule lookup: the first engine of the chain.
        procs = self.get_processors(source_program)
        return procs[0][1] if procs else None

    def _rules_signature(self):
        """(filename, mtime, size) for every rule file. The directory's own
        mtime is NOT enough: on Linux it only changes when a file is added,
        removed or renamed — editing a rule in place would never trigger a
        reload."""
        sig = []
        for f in sorted(os.listdir(self.rules_dir)):
            if f.endswith(".yaml"):
                try:
                    st = os.stat(os.path.join(self.rules_dir, f))
                    sig.append((f, st.st_mtime, st.st_size))
                except OSError:
                    continue
        return tuple(sig)

    def _config_signature(self):
        if not self.config_path:
            return None
        try:
            st = os.stat(self.config_path)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def _watch_loop(self):
        try:
            last_sig = self._rules_signature()
        except OSError:
            last_sig = None
        last_cfg = self._config_signature()
        while True:
            time.sleep(10)
            try:
                sig = self._rules_signature()
                if sig != last_sig:
                    last_sig = sig
                    self.reload()
                cfg = self._config_signature()
                if cfg != last_cfg:
                    last_cfg = cfg
                    self.reload_program_map()
            except Exception:
                pass
