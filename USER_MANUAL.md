# KAIROS — User Manual

Personal leisure activity planner. Learns what you enjoy, watches the weather and your goals, and nudges you when conditions are right.

---

## How it works

```
Your phone (GPS) → server → detect where you stopped → classify activity
                                                         → ask for rating ⭐
                                                         → learn your preference

Every morning at 06:30 → score all activities → send plan to Telegram
You: Approve / Reroll / Skip → system remembers your decision
```

The system never pushes unwanted activities — it proposes, you decide.

---

## Setup

### 1 — Overland (GPS tracking)

Install **Overland** on your phone (iOS / Android, free).

Open Settings → Trip URL and set the server's GPS URL:
```
https://<your-server>/gps
```
Which server? It depends on your environment:
- **Development:** `https://<your-ngrok-domain>/gps` — ngrok exposes your local app to the phone.
- **Production:** `https://<DOMAIN_NAME>/gps` — the Hostinger URL (e.g. `https://srv1169048.hstgr.cloud/gps`).

Set frequency to **every 30 seconds** while moving. The app batches pings and sends them when you have network. No action needed after that — just carry your phone.

### 2 — Telegram bot

Find **@PlanovaciBot** in Telegram and send `/start` once (to open the chat). The server will send all plans and feedback requests there.

### 3 — Dashboard

Open in any browser:
```
http://localhost:8000/dashboard        # Development
https://<DOMAIN_NAME>/dashboard       # Production (e.g. https://srv1169048.hstgr.cloud/dashboard)
```
```
(or replace `localhost:8000` with your server URL)

---

## Daily flow

### Morning plan (06:30)

You receive a Telegram message:

```
Today's clear spring afternoon is perfect for a round of golf.

• golf — conditions favor it; behind on goal cadence.
```

Three buttons appear:

| Button | Meaning |
|---|---|
| ✅ Approve | Accept the plan — system notes this as your intention |
| 🔄 Reroll | Reject and generate a new plan |
| ⏭️ Skip | No plan today |

### Activity detected

When you stay somewhere for more than 12 minutes and move away, the system detects a staypoint and asks:

```
Detected golf 10:15–13:30.
Rate it 1–5 (just reply, e.g. `4 great pace #content`).
```

Reply with a number 1–5. Optionally add a note and a `#emotion` tag:
- `5` — just five stars
- `4 good pace` — stars + note
- `3 too windy #frustrated` — stars + note + emotion

Your rating immediately updates the preference for that activity.

### Kairos window alert (hourly scan)

If conditions are unusually favorable for an activity you haven't done in a while, you may receive an unprompted nudge:

```
⏳ Window open for cold-water swimming (Clear, 8°C).
You haven't done it in 14 days — go?
```

---

## Dashboard

Open `http://localhost:8000/dashboard` to see everything at once.

### Sections

**Enrich** — Add new venues or events using plain text (see below).

**Conditions** — Current season, daylight status, weather, temperature pulled from the latest GPS ping.

**Today's Plan** — The current plan with status (proposed / approved / rejected), picks, and the reasoning behind each pick.

**Activity Scores** — All activities ranked by utility score. Columns:
- *Route*: **NOW** (good conditions today), **NEXT** (better later), **PARK** (blocked by weather/season), **ARCHIVE** (consistently low preference)
- *Preference*: your learned star-rating average (Beta posterior mean)
- *Fit*: how well today's conditions match the activity's requirements
- *Utility*: combined score used for ranking

**Goals** — Active goals with progress bars (e.g. "golf 3× per fortnight").

**Venues** — All known locations the GPS can match against.

**Session Log** — Every detected activity. Click ★ to rate sessions that haven't been rated yet.

**Feedback History** — All past ratings with notes and emotion tags.

**Recent Plans** — Last 10 plans with status history.

**GPS Ping Log** — Last 30 GPS pings. Session column: `…` = unassigned, 🏠 = home (suppressed), `#N` = matched session.

---

## Adding new venues and events

Use the **Enrich** panel at the top of the dashboard. Type a natural description:

```
Golf tournament in Hluboká nad Vltavou on 19.6.2026
New cycling trail at Prokopské údolí, Praha
Massage at Zen studio, Smíchov
```

Click **Extract & Save**. The system will:
1. Identify the venue, activity, and date using AI
2. Geocode the venue (look up coordinates)
3. Add the venue to GPS matching
4. Add a goal if an event date was mentioned

Multiple events can be entered at once (one per line or in a paragraph).

---

## Activity routing explained

Each activity gets routed every time the plan is built:

| Route | Condition |
|---|---|
| **NOW** | Fit ≥ 60%, not blocked by weather/season |
| **NEXT** | Fit 20–60% — good for later this week |
| **PARK** | Blocked by a rule (Rain blocks golf) or fit < 20% |
| **ARCHIVE** | ≥5 sessions rated and preference < 25% — no longer suggested |

A **block rule** sets utility to 0 regardless of preference. Example: Rain always blocks golf, cycling, and hike.

---

## Scoring formula

```
Utility = (w_pref × Preference + w_fit × Fit + w_prog × GoalProgress + w_expl × Exploration) × RuleFactor
```

- **Preference** — your Beta posterior mean from star ratings (starts at 0.5, updates with each ⭐)
- **Fit** — season / daylight / temperature match (0 to 1)
- **GoalProgress** — fraction of goal cadence still unmet (boosts activities you're behind on)
- **Exploration** — UCB bonus for activities you haven't tried recently
- **RuleFactor** — multiplied by activate/modulate rules; set to 0 if any block rule fires

Default weights: Pref=1.0, Fit=1.2, Progress=0.6, Exploration=0.4 (adjustable in `.env`).

---

## Telegram feedback format

```
<stars> [note text] [#emotion]
```

Examples:
```
5
4 legs felt heavy
3 too crowded #annoyed
5 perfect morning run #flow
```

Only the digit 1–5 is required. The `#tag` sets the emotion field in the database.

---

## Troubleshooting

**No plan received at 06:30**
- Check that `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`
- Check webhook: `GET https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
- Check server logs for scheduler errors

**GPS pings not arriving**
- Confirm Overland URL is set to `https://<your-domain>/gps`
- Check ngrok or reverse proxy is forwarding to port 8000
- Test: `GET /health` should return `{"status":"ok"}`

**Activity detected as wrong type**
- The nearest venue wins. Add or correct the venue via the Enrich panel.
- If no venue matches within radius, classification falls back to heuristics.

**Venue not geocoded after enrichment**
- `LOCATIONIQ_API_KEY` may be invalid or over quota. Check `.env` and re-submit the same text after fixing.
