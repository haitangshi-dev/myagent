## Description: <br>
Scans an OpenClaw environment and produces a beginner-friendly onboarding report with compatible skill recommendations and exact install commands. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[certainlogicai](https://clawhub.ai/user/certainlogicai) <br>

### License/Terms of Use: <br>
MIT-0 <br>


## Use Case: <br>
External OpenClaw users and developers use this skill to scan their local setup, identify compatible starter skills, and receive install commands or optional setup artifacts they can review before running. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: The skill scans local OpenClaw setup details and can write local reports, state files, team exports, or setup scripts. <br>
Mitigation: Install only if local scanning is acceptable, and review generated files, especially any setup script, before running or sharing them. <br>
Risk: Generated recommendations are heuristic and may include incompatible, deprecated, or opinionated skill choices. <br>
Mitigation: Treat recommendations as a starting point and confirm compatibility, licensing, and publisher claims before installing recommended skills. <br>
Risk: Server metadata and artifact files contain inconsistent package, version, and license signals. <br>
Mitigation: Verify the package identity, version, and license terms against the ClawHub release and artifact license files before relying on publisher claims. <br>


## Reference(s): <br>
- [ClawHub skill page](https://clawhub.ai/certainlogicai/skills/ai-agent-onboarding-wizard) <br>


## Skill Output: <br>
**Output Type(s):** [Text, Markdown, Shell commands, Configuration, Guidance] <br>
**Output Format:** [Markdown reports with inline shell commands and optional generated setup scripts or export files] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [May create local report, state, setup script, verification, weekly checkup, or team export files when those options are used] <br>

## Skill Version(s): <br>
1.0.1 (source: server release metadata; artifact files mention 2.1.x) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
