# Liza — How We Grow This

**From one device in one room, to an app used by thousands of students**
Written in plain language. No technical background needed.

| | |
|---|---|
| **Today** | One device. One program. Runs by itself, in one room. |
| **The plan** | An app where a student picks their class, and Liza teaches at that level |
| **Also needed** | A control panel for schools and for us, and a proper server behind it all |
| **Date** | 20 August 2026 |

---

## 1. Where we are today, honestly

The device works. It has been tested hard, and the record of that testing is in the companion document. But **it was built to be one device in one room**, and a few things about it cannot be sold to a thousand homes as they are:

| What it is now | Why that stops us growing |
|---|---|
| **The secret keys are stored on the device** | Every device would carry our billing keys. Anyone could take them off the device and spend our money. |
| **The conversation is saved in a file on the device** | Nothing syncs. Change the device and the student's history is gone. We cannot show a parent any progress. |
| **Settings are edited by hand on the device** | To change how she behaves, someone must physically connect to that device. |
| **There is no way to see how a device is doing** | Our biggest bug ever — a fifth of all sound being lost — was silent. If that happened on 500 devices tomorrow, **we would find out from complaints, not from data.** |
| **Everything is one program** | The part that listens to the room and the part that decides what to say are tangled together. To sell an app, those have to come apart. |

**None of this is a rewrite.** It is a reorganisation, and Section 8 lays out the order.

### The one sentence that shapes the whole plan

> **Physics stays on the device. Decisions move to the server.**

How loud the room is, where the microphone is, how to tell a voice from a fan — those are about *that specific room and that specific microphone*, and they must stay local. Everything else — what Liza knows, how she answers a Class 6 student versus a Class 11 student, what she is allowed to do — **belongs on our server, where we can change it for everyone at once.**

---

## 2. The plan in one picture

```
   ┌──────────────────────┐   ┌──────────────────────┐   ┌────────────────────┐
   │   STUDENT'S APP      │   │   PHYSICAL DEVICE    │   │   ADMIN CONSOLE    │
   │   (phone or tablet)  │   │   (the Pi we built)  │   │   (web page)       │
   ├──────────────────────┤   ├──────────────────────┤   ├────────────────────┤
   │  Pick your class     │   │  Hears the room      │   │  Manage students   │
   │  Talk / practice     │   │  Speaks the answer   │   │  Set the syllabus  │
   │  See your progress   │   │  Plays media         │   │  Watch the devices │
   └──────────┬───────────┘   └──────────┬───────────┘   └─────────┬──────────┘
              │                          │                         │
              └──────────────┬───────────┴─────────────────────────┘
                             │
                   ┌─────────▼──────────┐
                   │    OUR SERVER      │
                   │                    │
                   │  Who is this?      │  ← accounts and login
                   │  What class?       │  ← builds the right instructions
                   │  Ask the AI        │  ← we hold the keys, not the device
                   │  Save the progress │  ← history, mistakes, topics covered
                   │  Count the cost    │  ← what each student costs us
                   └────────────────────┘
```

**The important change:** today the device talks straight to the AI companies. In the new plan, **everything goes through our server first.** That one move is what makes accounts, class levels, progress reports, cost control and safety possible at all.

**The cost of that move:** it adds one extra stop to the journey, which costs a fraction of a second. Our previous report showed the wait is already 3.3 seconds, so we take this seriously — the fix is to put our servers in the same region as our users and pass the answer straight through as it arrives, rather than waiting for all of it. **Budget: under 0.15 seconds added.**

---

## 3. The main new feature: Liza knows what class you are in

This is the heart of the product plan. A student picks **Class 8, CBSE, Hindi medium** once, and everything changes.

### What actually changes when a class is selected

| What changes | Class 4 | Class 11 |
|---|---|---|
| **How deep the answer goes** | "Plants make their food from sunlight." | "Light reactions produce ATP and NADPH, which the Calvin cycle then uses." |
| **The words she expects to hear** | simple science words | organic chemistry, calculus, thermodynamics |
| **The examples she reaches for** | things in the house | things in an exam paper |
| **How long she talks** | two short sentences | a full explanation with a worked example |
| **What she treats as "off the syllabus"** | gently redirects | answers it, because seniors ask beyond the book |
| **How she marks you** | encouraging, one correction | exam-style, marks the specific error |

### Why this is easier than it sounds

**The device is already built this way.** Three pieces of it are already swapped in and out depending on the situation, and the class level slots into exactly the same places:

1. **The vocabulary hint.** Before listening, we already tell the speech system what words to expect — school science terms. That is *why* "mitochondria" is not heard as "microcontroller." **A Class 11 chemistry list and a Class 4 list are just different lists.**
2. **The teaching instructions.** We already swap these between Ask mode, Practice mode and Test mode. **A class level is one more thing to swap.**
3. **The order of the instructions.** Our previous report explained that the AI gives us a discount when the beginning of our message stays the same. A student's class **almost never changes**, so it sits high up where it stays discounted. Their question sits at the bottom. **The class feature costs us almost nothing per request.**

### What we need to build for it

- A **syllabus map**: for each class and each board (CBSE, ICSE, state boards), the list of subjects and topics
- A **vocabulary list per class**, feeding the speech system
- A **depth setting per class**, feeding the answer instructions
- A **screen in the app** where the student picks it, and a parent or teacher can lock it

---

## 4. The student app — what a user sees and does

### First time they open it

1. Sign in (phone number or a school-provided code)
2. **Pick class** — 1 to 12
3. **Pick board** — CBSE, ICSE, or state
4. **Pick language** — English, Hindi, or a mix
5. **Pick subjects** they want help with
6. Optional: pair a physical Liza device by scanning a code

### The screens

| Screen | What the student sees | What they can do |
|---|---|---|
| **Home** | A big talk button, what they studied recently, their streak | Start talking, jump back into a topic |
| **Talk** | Liza's face, what she is doing right now, live text of the conversation | Ask anything, interrupt her, switch mode, stop |
| **Modes** | Three choices: **Ask me** · **Practise with me** · **Test me** | Switch at any time — these already exist on the device |
| **My progress** | Topics covered, what they keep getting wrong, time spent | Tap any topic to revise it |
| **History** | Every past conversation, searchable | Replay an answer, delete a conversation |
| **Media** | Songs and videos they asked for | Play, stop |
| **Settings** | Language, voice speed, what Liza may access | Change language, change voice, **delete all my data** |

### What the student can change, and what they cannot

| They can change | They cannot change |
|---|---|
| Language and voice | Their class level, if a parent or school has locked it |
| Which subjects appear | The safety rules |
| Delete their own history | Another student's anything |
| Pause the microphone | Whether conversations are saved, if a school requires it |

### Where progress comes from — and this is the interesting part

**Test me mode already produces exactly the right data, and today we throw it away.**

When a student explains a topic from memory, Liza already gives back a structured verdict: what they got right, up to three specific mistakes, and one topic to revise. **Today that is spoken aloud and forgotten.**

In the app, each verdict is saved. After a few weeks that becomes:

- Which topics the student has actually covered
- Which mistakes they repeat
- Which subject is weakest
- A weekly summary a parent or teacher can read in thirty seconds

**We do not have to build a testing system. We have to start saving the one we already built.**

---

## 5. The admin console — what an administrator sees and does

Two kinds of administrator, with different powers:

- **School admin** — a teacher or coordinator managing their own students
- **Our team** — managing everything, including the school admins

### What a school admin can do

| Area | What they can do |
|---|---|
| **Students** | Add and remove students, set each one's class and board, group them into batches |
| **Class settings** | Lock a student's class level, choose which subjects are switched on |
| **Their own content** | Upload the school's own notes, question papers or syllabus so Liza teaches from those |
| **Progress** | See every student's progress, spot who is struggling, export a report |
| **Safety** | Review anything flagged, see what Liza refused to answer and why |
| **Devices** | See which physical devices are assigned to which classroom, and whether they are working |

### What our team can do, on top of that

| Area | What we can do | Why it matters |
|---|---|---|
| **Content library** | Edit the syllabus map, vocabulary lists and teaching instructions for every class and board | Improve every student's experience at once, with no update to install |
| **Device health** | See each device's microphone health, how much sound it is capturing, when it was last heard from | **This is the direct answer to our worst-ever bug.** A device losing sound would now raise an alert instead of a complaint |
| **Remote calibration** | Trigger a microphone recalibration on a device without visiting it | Rooms change. Microphones get moved. |
| **Safety review** | Read flagged conversations, adjust the rules, publish the change immediately | An education product with children needs this to be fast |
| **Cost and usage** | What each student, school and region costs us per month | See Section 9 |
| **Model routing** | Choose which AI serves which customer tier | Our testing already showed a version that is ten times cheaper. Switching is one setting |
| **Release control** | Roll a change out to 5% of devices, watch, then roll it out to everyone | Today a bad change would reach everybody at once |

### What nobody can do

- **Nobody can read a conversation without it being recorded that they did.** Every look at a student's data is logged.
- **Nobody can switch off the safety rules.** They can be tightened, never removed.
- **Nobody can see the raw voice recordings** — see Section 7.

---

## 6. What lives where

### On the device (the physical Liza)

Everything about **this room and this microphone:**

- Listening, and telling a voice from noise
- The measured microphone settings from our testing
- Detecting the wake word
- Playing sound out of the speaker
- The screen, the character animations, the buttons
- A small offline fallback: if the internet drops, she says so clearly instead of going silent

### In the app (phone or tablet)

- Login and account
- Class, board, language and subject choices
- The talk screen and the conversation
- Progress and history
- Settings and deleting your own data

### On the server — the front end

This is the part people look at:

- The **student web app**, for anyone without the physical device
- The **admin console**, for schools and for us
- The **parent view**, a simple weekly summary

### On the server — the back end

This is the part that does the work. Broken into pieces so each can grow separately:

| Piece | What it does |
|---|---|
| **Accounts** | Who is this, what school, what class, what are they allowed to do |
| **Conversation service** | Takes a question, builds the right instructions for that student's class, calls the AI, streams the answer back |
| **Provider routing** | Holds all the API keys, picks which AI service to use, retries when one fails, switches to a backup |
| **Progress service** | Saves the marks from Test me mode, builds the weekly summaries |
| **Content service** | The syllabus map, the vocabulary lists, the school's own uploaded material |
| **Safety service** | Checks requests, flags anything concerning, keeps the record |
| **Device service** | Registers devices, tracks their health, pushes settings and updates |
| **Metering** | Counts every request and what it cost, per student |

### On the server — the database and files

| Store | What goes in it |
|---|---|
| **Main database** | Accounts, classes, schools, devices, conversation text, progress records |
| **File storage** | Uploaded school material, the character animations, app assets |
| **Cache** | Answers to very common questions, so we do not pay twice for "what is photosynthesis" |
| **Background jobs** | Weekly summaries, cost rollups, device health alerts, cleaning out old data |

### Outside our server

The three AI services we buy: **speech-to-text**, **the AI brain**, and **the voice**. All three are behind our provider routing, so any one of them can be swapped without touching the app or the device.

---

## 7. Safety and privacy — this is a children's product

This section is not optional. Our users are children, in India, and the law here has real teeth.

| Rule we are building to | How we do it |
|---|---|
| **A parent or school must consent** before a child's account is created | Consent is part of sign-up and is recorded |
| **Voice recordings are not kept** | The audio is used to produce text and then deleted immediately. **The text is what we store**, and only if the account allows it |
| **A student can delete everything** | One button in settings, and it actually deletes — not just hides |
| **We do not sell data, and we do not train on it** | Written into the provider contracts, not just our policy page |
| **Every access is logged** | Any admin looking at a student's data leaves a record |
| **Data stays in India** | Servers in-region, which also helps speed |
| **Refusals are visible** | Everything Liza declined to answer is reviewable by the school, so nothing is silently hidden from parents |

**The safety rules already built into the device carry forward:** she refuses anything that could hurt someone, anything sexual, and anything targeting a real person. She cannot run administrator commands. She cannot leave the home folder. **Those are enforced in our own code, not by asking the AI nicely** — which was a deliberate decision from the start, and it is exactly what lets us stand behind it now.

---

## 8. The order we build it in

Each phase is useful on its own. Nothing here needs the phase after it to be worth doing.

### Phase 1 — Make it sellable *(the unglamorous one)*

**Nothing a user sees changes. Everything about how we run it changes.**

- Split the one program into a **device part** and a **service part**
- Move all keys off the device and onto our server
- Add accounts and login
- Add device health reporting, so a broken microphone raises an alert
- Add staged rollouts, so a bad change reaches 5% of devices and not 100%

> **Why first:** every single thing after this depends on it, and shipping the app without it means shipping our billing keys to strangers.

### Phase 2 — The app, and the class level

- Student app for Android and web
- Class, board and language selection
- The syllabus map and vocabulary lists for the first set of classes
- Conversations sync between the app and the device

> **What this unlocks:** we can sell to a student who does not own the hardware. **The app becomes the product; the device becomes the premium version.**

### Phase 3 — Progress

- Save the marks Test me mode already produces
- The progress screen
- Weekly summaries for parents

> **Why this matters commercially:** a parent will not pay monthly for a talking toy. **They will pay for evidence their child is learning.**

### Phase 4 — Schools

- The admin console
- Batches, teacher accounts, exportable reports
- Schools uploading their own material

> **What this unlocks:** one sale becomes forty students instead of one.

### Phase 5 — Running it cheaply at scale

- Cache the common answers
- Route different customer tiers to different AI services
- Regional servers
- Renegotiate the voice contract, which by then is our biggest bill

---

## 9. What the AI services cost us

> **Rates below are the published prices as of August 2026, checked against each provider. The only estimate is how much a student uses the product; the per-unit rates are real.**

### The three services we pay for

| Service | What it does | What we pay |
|---|---|---|
| **Speech-to-text** — Groq Whisper large-v3 | Turns the student's voice into text | **$0.111 per hour of audio** — but billed with a **10-second minimum per request** |
| **The AI brain** — Gemini 2.5 Flash | Works out the answer | **$0.30 per million words in**, **$2.50 per million words out** |
| **The voice** — Cartesia Sonic | Turns the answer into speech | **1 credit per character.** Works out at **$37 per million characters** on the $299/month plan, falling towards **$5 per million** at volume |

### What one question actually costs

**Assumptions:** the student speaks for about 5 seconds, Liza's instructions plus the conversation come to about 2,100 words, and her spoken answer is about 250 characters.

| Service | Cost per question | Share |
|---|---|---|
| Speech-to-text | $0.00031 | 3% |
| The AI brain | $0.00065 | 6% |
| **The voice** | **$0.00935** | **91%** |
| **Total** | **$0.0103** | |

> ### The single most important number in this document
> **The voice is 91% of what we pay.** The AI brain — the part everyone assumes is expensive — is 6%. Speech-to-text is 3%.
>
> **Every serious cost decision we make is a decision about the voice.**

### Two details that cost us more than they look

**The 10-second minimum.** Our recordings are about 5 seconds, but we are billed for 10 seconds every time. **We pay double on every single question**, and there is no way around it except making fewer requests. This is exactly why the media fix in our previous report matters: cutting from ~25 checks per song to ~4 saved 21 billed requests every time a student plays music.

**The AI brain price is already reduced.** The $0.00065 above includes the caching discount from our previous report. Without that fix it would be roughly double.

### Cost per student, per month

| How much they use it | Questions/month | Cost at the $299 plan | Cost at volume pricing |
|---|---|---|---|
| **Light** — a few questions a day | 50 | $0.52 | $0.11 |
| **Typical** — 8 questions a day | 200 | **$2.06** | **$0.44** |
| **Heavy** — 20 questions a day | 500 | $5.15 | $1.11 |

**That right-hand column is the business.** Moving the voice from list price to a volume contract takes a typical student from **$2.06 to $0.44 a month** — a 79% cut in the cost of running the product, from one negotiation.

### The levers, in the order we would pull them

| Lever | What it saves | Where it came from |
|---|---|---|
| **1. Volume voice contract** | Up to 79% of total cost | The published range is $37 down to $5 per million characters. This is a commercial negotiation, not an engineering project |
| **2. Shorter answers** | Cuts the 91% line directly | **Already done.** Answers now match the question instead of always being a lecture. A one-sentence answer costs a fifth of a four-sentence one. That fix was made for user experience — **it turns out to be our main cost control** |
| **3. Cache common answers** | Meaningful at scale | Thousands of students in the same class ask "what is photosynthesis". We should pay for that answer once, not ten thousand times |
| **4. Cheaper AI brain for free users** | ~$0.30 per student | Already tested: a version ten times cheaper, one line to switch |
| **5. Cheaper speech model for wake-word checks** | Small but free | Already done — the light model at $0.04/hr handles "Hey Liza", the full model handles real questions |

---

## 10. Subscription plans

> **Rupee prices assume about ₹88 to the dollar.**

### The plans

| | **Free** | **Plus** | **Family** | **School** |
|---|---|---|---|---|
| **Price** | ₹0 | **₹299/month**<br>or ₹2,999/year | **₹499/month**<br>or ₹4,999/year | **₹1,200 per student<br>per year** |
| **Who it is for** | Trying it out | One student | Up to 3 children | Whole classrooms |
| **Questions** | 10 a day | Unlimited* | Unlimited* | Unlimited* |
| **Ask me mode** | ✓ | ✓ | ✓ | ✓ |
| **Practise with me** | — | ✓ | ✓ | ✓ |
| **Test me + marking** | — | ✓ | ✓ | ✓ |
| **Video library** | 3 a day | **Full access** | **Full access** | **Full access** |
| **Study material** | — | **Full access** | **Full access** | **Full + school's own** |
| **Progress reports** | — | ✓ | ✓ per child | ✓ per class |
| **Weekly parent summary** | — | ✓ | ✓ | Teacher reports |
| **Offline downloads** | — | ✓ | ✓ | ✓ |
| **Answer speed** | Standard | **Priority** | **Priority** | **Priority** |
| **History kept** | 7 days | Forever | Forever | Per school policy |
| **Physical device pairing** | — | ✓ | ✓ | ✓ |
| **Admin console** | — | — | Parent view | **Full console** |

<small>*Fair use applies — see the note below.</small>

### What a subscription actually unlocks

**The video library.** Free users get three videos a day, pulled from a general search. Subscribers get the **curated, syllabus-mapped library**: every video tied to a topic in their class and board, ad-free, and safe to leave a child alone with. **This is the single most requested thing, and it is the clearest reason to pay.**

**Study material.** Notes, worksheets, solved examples and past papers for their exact class and board. Downloadable for offline use. Liza can teach directly from them — *"open my chapter 6 notes and test me on it."*

**The two study modes.** Free gives Ask me, which is the demo. **Practise with me and Test me are the product** — they are what turns a question-answering toy into something a student uses every evening. They are also where progress data comes from.

**Progress and the weekly parent summary.** The report that tells a parent their child covered four topics, keeps confusing two concepts, and should revise one specific thing this week. **This is what renews the subscription** — not the talking, the evidence.

**Priority speed.** Subscribers route to the faster AI. Free users route to the cheaper one, which is about 1.6 seconds slower to start answering. That is a real difference the user can feel, and it costs us nothing to offer.

### Hardware

| | Price | Includes |
|---|---|---|
| **Liza device** | ₹9,999 one-time | The device, plus **12 months of Plus included** |
| Replacement / additional | ₹8,999 | Device only, pairs to an existing subscription |

### Does the pricing work?

At a **typical** 8 questions a day:

| Plan | Revenue/month | Cost at list price | Cost at volume pricing |
|---|---|---|---|
| **Plus ₹299** | $3.40 | $2.06 → **39% margin** | $0.44 → **87% margin** |
| **Family ₹499** (2 children) | $5.67 | $4.12 → **27% margin** | $0.88 → **84% margin** |
| **School ₹1,200/yr** | $1.14 | $2.06 → **loses money** | $0.44 → **61% margin** |

> ### The honest conclusion
> **At list price, Plus works and School does not.** The school tier — the one that turns a single sale into forty students — **only becomes profitable once we have the volume voice contract.**
>
> That is not a reason to change the plan. It is a reason to know the order: **sign the voice contract before signing the schools.**

### Two protections we build in from day one

**A fair-use cap.** "Unlimited" is capped at 40 questions a day. A student at that level costs us more than they pay, and roughly one in fifty users will be there. The cap is generous enough that almost nobody meets it, and it stops one heavy user costing what ten normal ones pay.

**Free users cost us almost nothing.** Ten questions a day, routed to the cheaper AI and the cheaper voice settings, keeps a free user at roughly **$0.11 a month**. That is a cheap enough trial to give away freely, which is what conversion in the Indian education market depends on.

---

## 11. What could go wrong

| Risk | How serious | What we do about it |
|---|---|---|
| **The voice contract does not improve** | **Highest** | Everything in Section 9 and 10 turns on this. Fallbacks: shorter answers, aggressive caching, a second voice provider quoted against the first. **We have already swapped the voice once, so we know it is a two-week job, not a rebuild** |
| An AI provider raises prices or shuts down | High | Everything goes through our own routing layer. Swapping a provider is a settings change |
| A child asks something we should have refused | Very high, reputationally | Rules in our own code, not in the AI's instructions. Full review log. A rule change reaches everyone within minutes |
| Devices fail quietly in the field | High | Health reporting from Phase 1. **Our worst bug was invisible once — never again** |
| Free users cost more than expected | Medium | The daily cap and cheap routing are enforced on our server, not in the app, so they cannot be bypassed |
| We add features and the 3.3-second wait grows | Medium | Latency measured and reported on every release, exactly as in the current report |
| Schools want their own content and we cannot support it | Medium | Phase 4 is built for it |

---

## 12. The short version

**What we have** is a device that works, that has been measured properly, and whose every setting can be defended.

**What we are building** is an app where a student says what class they are in and gets taught at that level — backed by a server we control, with a console that lets a school run it and lets us see every device.

**What we charge** is ₹299 a month for a student, ₹499 for a family, and ₹1,200 a year per student for a school — unlocking the full video library, the study material, the two study modes that make it a habit, and the progress reports that make a parent renew.

**Three things make it achievable rather than aspirational:**

1. **The hard part is done.** Getting a cheap microphone to hear a child across a room, in two languages, with an answer in about three seconds, took thirty-three rounds of measurement. **That work does not have to be repeated for the app.**
2. **The class feature fits the existing design.** The vocabulary hints, the teaching instructions and the answer depth are already swapped in and out on every request. A class level is one more thing to swap.
3. **The progress feature is already half built.** Test me mode already marks a student properly, out loud, today. **We just have to save it.**

**And one thing to get right first:** the voice is 91% of what we pay. **The single highest-value commercial action available to this company is a volume contract with the voice provider** — it takes the cost of a typical student from $2.06 a month to $0.44, and it is what makes the school tier profitable.

---

*Companion document: "Liza — What We Tested, and What Changed", which records the measurements this plan builds on.*
