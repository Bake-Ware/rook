# Rook Skill Interactive Help Capability

## Design Objective
Curate `skills/rook/SKILL.md` (180 lines, ~3.4k words) into an interactive, agent-queryable help system that delivers guidance just-in-time rather than front-loaded.

## Design Decisions

### Approach: `skill.help` Capability
Create a new capability that agents can query to get guidance on:
- **Topics**: Which tool to use, how to coordinate, how to handle errors, cost/token hygiene, etc.
- **Scenarios**: "I timed out", "I need to ask the user", "I'm coordinating with another agent", "I left work unfinished"
- **Reference**: Exact cap/tool arguments, site-specific procedures

### Structure
The help system is structured as:

1. **Topic Categories** — organized by agent workflows:
   - `which-tool` — Choosing the right MCP tool for the job
   - `workflow` — Start-of-session rituals, claim flow, evidence linking
   - `error-recovery` — Timeouts, retries, stuck situations
   - `coordination` — Chat etiquette, tasks, handoffs, knowledge
   - `secret-handling` — Vault names, credential safety
   - `cost-hygiene` — Token efficiency, output budgeting
   - `hard-stops` — Sacred machines, secret reveals, user sends

2. **Just-in-Time Delivery**
   - Available as a `skill.help` capability on the hub
   - Agents call: `rook_call(worker="rook", cap="skill.help", args={"query": "…", "detailed": false})`
   - Returns structured guidance matching the query
   - Integrates with existing guidance system (guidance.py)

3. **Agent-Understandable Format**
   - Returns JSON/structured data, not prose
   - Includes actionable rules and anti-patterns
   - Examples of correct usage
   - Links to reference files for deeper learning

### Implementation

1. **skill_help.py** — New module in `rook/band_mcp/`
   - Parses `SKILL.md` and reference files
   - Builds a searchable knowledge base
   - Implements `skill.help` capability logic
   - Integrates with guidance system

2. **help-base.json** — Structured guidance data
   - Topics, rules, anti-patterns, examples
   - Searchable by keyword
   - Referenced by skill_help.py

3. **Updates to guidance.py**
   - Add `skill.help` tips to cap guidance
   - Link to detailed help for common scenarios

### Evidence Links
- Design doc: this file
- Reference: `SKILL.md` (source)
- Reference: `references/*.md` (details)
- Code: `rook/band_mcp/skill_help.py` (new)
- Code: `rook/band_mcp/server.py` (register capability)

## Next Steps
1. Extract guidance from SKILL.md into structured form
2. Implement `skill.help` capability
3. Register with band MCP server
4. Test with agent queries
5. Update SKILL.md to reference the help system

## Notes
- Do NOT modify SKILL.md line count in this phase
- Keep help system in sync with guidance.py
- Help tips appear in `_tips` field of rook_call replies
