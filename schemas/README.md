# 911Bench JSON Schemas

Standalone JSON Schema files extracted from the 911Bench Architecture Document v4.
These schemas are the normative reference for all data structures in the system.

## Schema Files

| File | Describes | Architecture Doc Section |
|------|-----------|------------------------|
| `action_proposal.schema.json` | ActionProposal (agent → governance) | 3.3.1 |
| `action_outcome.schema.json` | ActionOutcome (governance → agent) | 3.3.2 |
| `evidence_ref.schema.json` | Evidence reference (two-level type system) | 3.3.3 |
| `events.schema.json` | All event types in `_events.ndjson` | 2.4 |
| `caller.schema.json` | Caller profile scenario seed | 2.2.1 |
| `incident.schema.json` | Incident context scenario seed | 2.2.2 |
| `qa_template.schema.json` | QA rubric template | 2.2.3 |
| `governance_policy.schema.json` | Governance policy YAML (validated as JSON) | 3.4.2 |
| `action_registry.schema.json` | Action class registry | 3.4.1 |
| `domain_evidence_config.schema.json` | Domain-specific evidence subtypes | 3.3.3 |

## Usage

### Validation
```python
import json
import jsonschema

with open("schemas/action_proposal.schema.json") as f:
    schema = json.load(f)

proposal = { ... }  # ActionProposal dict
jsonschema.validate(proposal, schema)
```

### With the Governance Server
The Governance server loads these schemas at startup and uses them for:
- ActionProposal validation (enforcement pipeline step 1: Validate)
- Policy YAML validation (Section 3.5: YAML Ingestion)
- Evidence validation (Section 3.3.3: Evidence Model)
- Event schema validation (event logger)

### With Claude Code
When implementing a component, reference the relevant schema file:
```
"I'm implementing the enforcement engine. Here is the ActionProposal schema: [schemas/action_proposal.schema.json]
and the ActionOutcome schema: [schemas/action_outcome.schema.json]. Validate incoming proposals against the schema
at the Validate step."
```

## Governance Policy Note
The governance policy schema (`governance_policy.schema.json`) validates YAML policy files
after they are parsed to JSON. The YAML is the authoring format; JSON Schema validates
the parsed structure. Policy files map to the paper's authority policy tuple:
π = ⟨a, r, A, O, E⟩ where:
- a = `name` (action class)
- r = `controlling_entity` (role + escalation target)  
- A = `autonomy_level` (A0–A5)
- O = `operators` (Prohibit, Bound, Escalate, Checkpoint, Audit)
- E = `evidence_requirements` (checkpoint presentation + audit capture)

## Evidence Model Note
The evidence model uses a two-level type system:
- **Abstract categories** (in `evidence_ref.schema.json`): `human_communication`,
  `operational_record`, `procedural_reference`, `sensor_or_signal`, `external_source`
- **Domain subtypes** (in `domain_evidence_config.schema.json`): e.g., `transcript_span`,
  `entity_extract`, `sop_ref` for the ECC domain

The action registry's `required_evidence` references abstract categories.
Agents populate `type` with domain subtypes. The validator checks that
subtypes belong to required categories.
