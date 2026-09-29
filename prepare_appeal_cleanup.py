"""Inventory proposed research deletions. Never deletes, moves, or git-stages files."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from config import ROOT
import group_scout


def main():
    protected_prompts={p for files in group_scout.PROMPT_SETS.values() for p in files.values()}
    protected_prompts.update((ROOT/'prompts').glob('scout_v*.md'))
    # This old helper is still exercised by the legacy runner's compatibility tests.
    held_back={ROOT/'notebooks/refinement/verify_group.py'}
    candidates={}
    for p in (ROOT/'notebooks/refinement').rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts and p not in held_back:
            candidates[p]='Experiment logs, audits, samples and research utilities'
    for p in ROOT.glob('*_lab.py'):candidates[p]='Retired experiment runner'
    for name in ['scout_proxy.py','frozen_first_pass.py','group_review.py','group_source_review.py','focused_export.py','research.py','rites next to this script#']:
        p=ROOT/name
        if p.is_file():candidates[p]='Superseded research entry point or scratch file'
    for p in (ROOT/'prompts').glob('*.md'):
        if p not in protected_prompts:candidates[p]='Superseded experiment prompt; selected contract preserved in prompts/appeals'
    for name in ['test_scout_proxy.py','test_agent_lab.py','test_brief_lab.py','test_gate_lab.py','test_duration_bounds_lab.py',
                 'test_group_review.py','test_group_source_review.py','test_dump_group.py',
                 'test_focused_export.py','test_research.py']:
        p=ROOT/'tests'/name
        if p.is_file():candidates[p]='Tests only for retired research entry points'
    for pattern in ['SCOUTING*.md','GROUP-SCOUTING.md','REFINEMENT*.md','FOCUSED*.md','EXTRACT-REVIEW.md']:
        for p in (ROOT/'docs').glob(pattern):candidates[p]='Superseded notes; conclusions preserved in APPEALS-RESEARCH.md'
    rows=[]
    for p,reason in sorted(candidates.items()):
        data=p.read_bytes()
        rows.append({'path':str(p.relative_to(ROOT)),'bytes':len(data),
                     'sha256':hashlib.sha256(data).hexdigest(),'reason':reason})
    value={'status':'proposed_only_not_deleted_or_git_staged','files':rows,
           'total_files':len(rows),'total_bytes':sum(r['bytes'] for r in rows),
           'retained_compatibility_helper':'notebooks/refinement/verify_group.py',
           'protected':['in/','data/','out/','prompts/appeals/','appeals.py','appeals.html','appeals.css','appeals.js','appeal_evidence.py',
                        'appeal_runtime.py','appeal_viewer.py','tests/test_appeals.py','tests/test_appeal_viewer.py','docs/APPEALS*.md',
                        'docs/appeals-cost-projection.json','group_scout.py','scout.py','groups.py',
                        'claims_server.py','review_server.py','json_repair.py']}
    path=ROOT/'docs/appeals-deletion-manifest.json'
    path.write_text(json.dumps(value,indent=2)+'\n')
    counts=Counter(r['reason'] for r in rows)
    sizes=Counter()
    for row in rows:sizes[row['reason']]+=row['bytes']
    table='\n'.join(f'| {k} | {counts[k]} | {sizes[k]/1024/1024:.2f} |' for k in counts)
    (ROOT/'docs/APPEALS-DELETION-PLAN.md').write_text(f'''# Research deletion proposal

**Prepared only. No files have been deleted, moved, or staged for deletion in Git.**
The user's existing index is untouched. This is a reviewable inventory for a later
authorized deletion, not a cleanup command to execute now.

**{len(rows):,} files, {value['total_bytes']/1024/1024:.2f} MiB** are proposed for removal.
Every exact path, size, SHA-256 and reason is in
[appeals-deletion-manifest.json](appeals-deletion-manifest.json).

| Category | Files | MiB |
|---|---:|---:|
{table}

## Preserved

- Original PDFs, archive/text/group/scout databases, all production data and new
  `data/appeals/` run logs, frozen prompts/sources and final exports.
- The selected six prompts under `prompts/appeals/`, supported runner/visualizer,
  evidence validators, JSON repair, direct API client, and their active tests.
- `APPEALS.md`, `APPEALS-RESEARCH.md`, `APPEALS-RELEASE-CHECK.md`, and the cost
  calibration snapshot preserve the operating guide, research decisions and results.
- Shared older modules `group_scout.py`, `scout.py`, `claims_server.py` and
  `review_server.py` still provide validation, source access or PDF serving. Their
  compatibility prompts/tests and `notebooks/refinement/verify_group.py` remain;
  these are software dependencies, not deletion-ready logs.
- Upstream ingestion/text documentation and tools are outside this cleanup.

The old `research-export`/experimental commands become unavailable after their
listed scripts are deleted; the supported replacement is `appeals.py export`.
No new production path imports an experiment runner or reads research notebooks.
For later deletion, first verify these hashes still match: changed/new files need
review instead of blindly applying a stale manifest. Regenerate this list with
`python prepare_appeal_cleanup.py`. It never performs deletion or Git operations.
''')
    print(f'{len(rows)} proposed files; {value["total_bytes"]} bytes; nothing deleted')


if __name__=='__main__':main()
