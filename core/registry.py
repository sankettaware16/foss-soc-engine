import os
import yaml
import time
from threading import Thread
from .engine import UniversalEngine

class RuleRegistry:
    def __init__(self, rules_dir, program_map):
        self.rules_dir = rules_dir
        self.program_map = program_map 
        self.engines = {} 
        self.reload()
        

        self.watcher = Thread(target=self._watch_loop, daemon=True)
        self.watcher.start()

    def reload(self):
        print("Loading Generic Rules...")
        new_engines = {}
        
        if not os.path.exists(self.rules_dir):
            print(f" Rules dir {self.rules_dir} not found.")
            return

        for f in os.listdir(self.rules_dir):
            if f.endswith(".yaml"):
                try:
                    with open(os.path.join(self.rules_dir, f), 'r') as yml:
                        rule_config = yaml.safe_load(yml)
                        
              
                        pattern_name = rule_config.get('pattern_name', f.replace('.yaml', ''))
                        
                        new_engines[pattern_name] = UniversalEngine(rule_config)
                        print(f"    Loaded Rule: {f} -> [Pattern: {pattern_name}]")
                        
                except Exception as e:
                    print(f"    Error loading {f}: {e}")
        
        self.engines = new_engines

    def get_processor(self, source_program):
        """
        The Magic: Maps specific Program -> Generic Pattern -> Engine
        """
     
        pattern_name = self.program_map.get(source_program)
        
        if not pattern_name:
            
            pattern_name = source_program
            
      
        return self.engines.get(pattern_name)

    def _watch_loop(self):
        last_mtime = 0
        while True:
            time.sleep(10)
            try:
                current_mtime = os.stat(self.rules_dir).st_mtime
                if current_mtime != last_mtime:
                    last_mtime = current_mtime
                    self.reload()
            except:
                pass
