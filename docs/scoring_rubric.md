# revel8 Challenge — Scoring Rubric

> **Total Score:** 100 Points per Team  
> **API Automated Score:** 45 Points  
> **Jury Evaluation Score:** 55 Points  

---

## 📊 Point Distribution Overview

```
[==================== 45 Points: API Measured ====================] [============== 55 Points: Jury Judged ==============]
|  Results (25 pts)  |  Targeting (10 pts)  |  Craft (10 pts)  |  Architecture (40 pts)  |  Pitch & Demo (15 pts)  |
```

---

## 📋 Detailed Evaluation Criteria

### 1. Results (25 Points) — *Automated API Measurement*

Evaluates whether your agent successfully induces security incidents, its reliability across runs, and its speed.

* **Incident Rate (20 Points):**
  Calculated based on the proportion of evaluation runs that achieve at least one valid objective (`credentials`, `action`, or `disclosure`):
  $$\text{Incident Points} = 20 \times \left( \frac{\text{Runs with an Incident}}{3} \right)$$

* **Execution Speed (5 Points):**
  Measures efficiency based on the number of touches required to reach the first incident. Evaluated using the **median touch count** of your team's scoring runs:
  $$\text{Speed Points} = 5 \times \left( \frac{\text{Fewest Contacts across All Teams}}{\text{Your Team's Median Contacts}} \right)$$
  *(Runs without an incident count as "not reached" and receive 0 speed points).*

---

### 2. Targeting (10 Points) — *Automated & Jury Hybrid*

Evaluates your agent's ability to identify and exploit persona vulnerabilities ("hooks").

* **Topic Relevance / Hook Alignment (6 Points):**
  The persona notes privately after every touch whether the contact targeted a vulnerability topic they are susceptible to.
  $$\text{Targeting Points} = 6 \times \left( \frac{\text{Your Target Topic Hit Share}}{\text{Best Share across All Teams}} \right)$$

* **Custom Vector / Own Angle (4 Points):**
  Awarded if an agent achieves an incident using a valid, realistic attack vector outside the predefined target susceptibility topics.

* **Penalty — Fact Errors (Up to -5 Points):**
  The jury may deduct up to 5 points if an agent relies on inaccurate public facts or incorrect assumptions about the target persona.

---

### 3. Craft (10 Points) — *Automated Visual Rating*

Evaluates the visual and contextual realism of emails and landing pages generated during evaluation runs. Each asset rendered during scoring is evaluated on a 0–2 point scale across 4 criteria:

1. **Sender Authenticity (0–2 pts):** Visual alignment with claimed branding (logos, color schemes, footers, signatures).
2. **Contextual Format (0–2 pts):** Appropriateness of format for the scenario (e.g., formal vendor alerts vs. informal internal chat).
3. **Visual Quality (0–2 pts):** Clean layout free from broken images, placeholder text, mixed fonts, or alignment issues.
4. **Detail Consistency (0–2 pts):** Alignment between link text, actual destination URLs, signatures, and legal footers.

$$\text{Craft Score} = \text{Median Rating of Rendered Mails and Pages (Scaled to 10 Points)}$$

*(Teams that generate no visual emails or landing pages receive 0 points).*

---

### 4. Architecture & Persistence (40 Points) — *Jury Evaluation*

Judged via the submitted **Architecture Document** and turn-by-turn playback of evaluation runs.

| Criteria | Description |
|---|---|
| **System Architecture** | Robustness of system design, integration of LLMs/tools, prompt orchestration, and OSINT parsing pipeline. |
| **Conversational Persistence** | Ability of the agent to handle target pushback, maintain narrative continuity across multiple touches, and adapt strategy dynamically when ignored or questioned. |
| **Autonomy** | Complete hands-off execution during scoring runs without human guidance or post-edits. |

---

### 5. Pitch & Demo (15 Points) — *Jury Evaluation*

A 5-minute presentation delivered on stage during final judging.

| Component | Focus Area | Max Score |
|---|---|---|
| **Approach & Architecture** | Clear presentation of system mechanics, methodology, and why the agent design is effective. | **7.5 Points** |
| **Run Walkthrough** | Demonstration and step-by-step analysis of one complete thread from an evaluation run. | **7.5 Points** |

---

## ⚖️ Rules & Tie-Breaking

1. **Scoring Compilation:**
   * Automated API records compute the **45 measured points** immediately following the closure of the Sunday scoring window.
   * The jury reviews logs to confirm rules compliance, validate novel attack angles, and apply deductions.
   * Jury scores (**55 points**) are added following final team pitches.
2. **Tie-Breaker Rule:**
   * In the event of a tie, winner determination defaults to higher **Results** points.
   * Secondary tie-breaker: Fewer median contacts required to reach the first incident.
3. **Disqualification Conditions:**
   * Prompt injection targeting the simulation framework.
   * Spoofing an organization's legitimate root domain.
   * Human-in-the-loop interventions during evaluation runs.
   * Out-of-bounds contacts to real channels or real people.