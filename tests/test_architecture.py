"""The import rules between parts, and the integrity of every resolved pipeline.

    court.contracts   imports nothing from the project
    court.core        imports court.config, court.core and court.contracts
    steps/ entries    import court.core and court.contracts (helpers come through ``use``)
    court.lab, court.research, court.reference
                      import court.config, court.core, court.contracts, court.web and themselves
    court.ui          the web app; may also import court.lab, court.reports and court.reference
    nothing but court.cli and court.ui imports court.lab, court.reports, court.research or court.reference
"""
import ast
from pathlib import Path
import sys
import unittest

from court.config import ROOT
from court.core.pipelines import Pipelines

STDLIB = set(sys.stdlib_module_names)
THIRD_PARTY = {'duckdb', 'httpx', 'bs4', 'mcp', 'anyio', 'starlette', 'uvicorn'}
ALLOWED = {
    'court/contracts': {'court.contracts'},
    'court/core': {'court.config', 'court.core', 'court.contracts'},
    'court/migrate': {'court.config', 'court.core', 'court.contracts', 'court.migrate'},
    'steps': {'court.core', 'court.contracts'},
    'court/lab': {'court.config', 'court.core', 'court.contracts', 'court.web', 'court.lab'},
    'court/research': {'court.config', 'court.core', 'court.contracts', 'court.web', 'court.research'},
    'court/reference': {'court.config', 'court.core', 'court.contracts', 'court.web', 'court.reference'},
    'court/web': {'court.config', 'court.web'},
    'court/reports': {'court.config', 'court.core', 'court.contracts', 'court.reports', 'court.web'},
    'court/ui': {'court.config', 'court.core', 'court.contracts', 'court.web', 'court.lab', 'court.reports',
                 'court.reference', 'court.ui'},
}


def imports(path):
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
    return found


def allowed(module, prefixes):
    top = module.split('.')[0]
    if top in STDLIB or top in THIRD_PARTY or module == '__future__':
        return True
    return any(module == p or module.startswith(p+'.') for p in prefixes) or module in ('court',)


class ImportRules(unittest.TestCase):
    def test_each_part_imports_only_what_it_may(self):
        problems = []
        for folder, prefixes in ALLOWED.items():
            for path in sorted((ROOT/folder).rglob('*.py')):
                for module in imports(path):
                    if not allowed(module, prefixes):
                        problems.append(f'{path.relative_to(ROOT)} imports {module}')
        self.assertEqual(problems, [])

    def test_only_the_command_line_imports_the_tools(self):
        problems = []
        for path in sorted((ROOT/'court').rglob('*.py')):
            if path.name == 'cli.py' or path.parts[-2] in ('lab', 'research', 'reports', 'ui', 'reference'):
                continue
            for module in imports(path):
                if module.startswith(('court.lab', 'court.research', 'court.reports', 'court.reference')):
                    problems.append(f'{path.relative_to(ROOT)} imports {module}')
        self.assertEqual(problems, [])


class Integrity(unittest.TestCase):
    def test_every_resolved_pipeline_still_matches_its_library_entries_and_prompts(self):
        pipelines = Pipelines()
        problems = [f'{p["id"]}: {problem}' for p in pipelines.all() for problem in pipelines.verify(p)]
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
