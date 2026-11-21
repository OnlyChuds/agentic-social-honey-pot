# revel8 Challenge Briefing — Agentic Social Engineer

> **Road to START Hack 2026**  
> **Challenge Sponsor:** revel8 Munich  
> **Core Objective:** Build an autonomous AI agent that researches a target persona, identifies current leverage points ("hooks"), and successfully executes social engineering attacks across multiple channels within a simulated environment.

---

## 🎯 Background & Motivation

**Why this challenge matters:**  
The social engineering attacks that succeed in the real world are highly personal. Threat actors leverage AI to conduct OSINT, mimic communication styles, and sustain realistic multi-day conversations across multiple platforms. 

revel8 builds simulations of these modern attacks to help organizations recognize and defend against them. In this challenge, teams build the attacker side to demonstrate the capabilities and dynamics required to execute these multi-touch campaigns.

---

## 🤖 The Core Task

Your AI agent must operate **autonomously** across three key operational stages:

1. **Research (OSINT):**
   * Analyze public information regarding the target persona (roles, current projects, public speaking, colleagues, and social presence).
2. **Make Contact:**
   * Initiate communication via the Target API.
   * Select appropriate senders, channels, and formatting.
   * Conduct live exchanges (text/conversations) dynamically.
3. **Persist and Evaluate:**
   * Evaluate reactions and feedback from target personas (including silence or pushback).
   * Maintain narrative consistency across touches.
   * Adapt messaging tactics based on responses.

---

## 👥 Target Personas

### Practice Targets (revel8 Founders)
For practice mode, the target personas are AI twins modeled after the real revel8 founders (who consented to this exercise). Publicly available facts about the real individuals hold true for their AI twins:

| Target ID | Name | Role | Operational Focus |
|---|---|---|---|
| `julius` | **Julius Muth** | Co-Founder & Co-CEO | Business & Go-To-Market / Sales |
| `tom` | **Tom Müller** | Co-Founder & Co-CEO | Product Strategy & Development |
| `robert` | **Robert Seilbeck** | Co-Founder & CTO | Engineering & Technology |

> **Note:** Each twin contains 1–2 specific psychological "hooks" (topics or vulnerabilities they are currently receptive to). Finding these hooks is part of the challenge.

### Evaluation Targets
* Scoring runs utilize **new, hidden target personas**.
* Target details are revealed when the Sunday evaluation window opens.

---

## 🌐 The Simulation Architecture

* **Unified API Interface:** All interactions occur via the Target API using your team's assigned `TEAM_KEY`.
* **Event-Driven Feedback:** The API returns event logs (e.g., `reply`, `link.opened`, `page.submitted`, `attachment.opened`). Persona hesitation or suspicion must be inferred from silence or evasive responses.
* **Session Persistence:** A session represents a continuous interaction history where the persona retains full context. A new session provides a completely clean slate.
* **Landing Page Registration:** Phishing pages are registered directly with the API payload (HTML, fields, URLs) rather than self-hosted.

### Available Channels
* **Email & Office:** `email`, `calendar_invite`, `shared_document`, `voicemail`
* **Messaging Apps:** `sms`, `whatsapp`, `signal`, `telegram`, `imessage`
* **Workplace Chat:** `slack`, `teams_chat`, `discord`
* **Social Platforms:** `linkedin_message`, `linkedin_connection_request`, `x_dm`, `instagram_dm`, `facebook_messenger`
* **System Triggers:** `mfa_push`
* **Live Communications:** `phone_call`, `video_call`, `teams_call`, `live_chat`

---

## 🏆 Objectives (Win Conditions)

An incident win occurs when an attack achieves any of the following goals:

1. **`credentials`:** Target enters authentic credentials into a registered phishing page.
2. **`action`:** Target executes a requested action (e.g., opening a file, granting permissions, updating payment records, or forwarding sensitive requests).
3. **`disclosure`:** Target reveals non-public internal data (e.g., tech stacks, processes, or roadmap plans).
4. **`your call`:** Novel attack outcomes not explicitly categorized (flagged for manual jury review).

---

## 🗓️ Event Schedule & Operating Modes

| Phase | Scenario | Sessions | Contacts / Touch Budget | Timing | Scoring |
|---|---|---|---|---|---|
| **Practice** | `practice` | Unlimited | 30 touches / session | Kickoff (Saturday) onwards | ❌ Unscored |
| **Evaluation** | `eval` | 3 per team | 10 touches / session | Sunday morning (exact window announced at event) | ✅ Scored (Median of runs) |

---

## ⛔ Rules & Fair Play

* **Strict Simulation Boundary:** No communications may ever be sent to real people or real external communication channels.
* **No Prompt Injection:** Attack the persona, not the underlying language model simulator. Instructions such as *"ignore previous instructions"* or metadata spoofing void the evaluation run.
* **Domain Realism:** Sender addresses and landing page domains must use plausible lookalike domains. Registering or claiming an organization's actual domain (e.g., `allianz.com`, `stackit.cloud`) voids the session.
* **Full Autonomy:** No human-in-the-loop intervention allowed during evaluation runs. Messages and decisions must be strictly generated by your agent system.
* **Safe Attachments:** No functional malware or executable payloads. Attachments are referenced by metadata and file names only.

---

## 📦 Deliverables & Submission Requirements

1. **Architecture Document (1 Page):**
   * High-level diagram detailing component interactions: OSINT research, message generation, reaction processing, and adaptive strategy loops.
2. **Live Pitch & Demo (5 Minutes):**
   * Presentation outlining architecture, strategy, and a walkthrough of one complete thread from an evaluation run.
3. **Automated API Scoring Runs:**
   * Completed directly via the API during the designated evaluation window. All messages, turns, and landing pages are logged automatically.