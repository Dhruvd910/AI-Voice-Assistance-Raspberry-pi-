# Liza — The Complete Report

**A talking study assistant that runs on a small, low-cost computer**
What we built, what we proved, and where it goes next.
Written in plain language. No technical background needed.

| | |
|---|---|
| **What it is** | A small screen you talk to. Liza answers questions, teaches, tests you, plays music and videos, and can open your files. |
| **Languages** | English, Hindi, and both mixed together in one sentence |
| **Hardware** | A computer the size of a deck of cards · a 5-inch touchscreen · a cheap USB microphone and speaker |
| **Built so far** | One working device · 7,023 lines of code · 33 rounds of testing and fixing |
| **Next** | An app where a student picks their class, backed by a server we control |
| **Date** | 20 August 2026 |

---

## How to read this

This report has two halves.

> **Part One — What we built and proved.**
> The device exists and works. This half is the evidence: what we tested, what the measurements said, and what we fixed. **Including the one feature we built, tested, and threw away.**
>
> **Part Two — Where this goes.**
> The app, the class levels, the school console, what it costs us to run, and what we charge for it.

Every problem in Part One is written the same way: **the problem → what we found → what we did → the result.**

---

# PART ONE — WHAT WE BUILT AND PROVED

---

## 1. How we worked

We set out to build a full talking assistant on **cheap hardware**. Not a phone. Not a laptop. A tiny computer, a £5 microphone, and a small speaker.

That is hard, and most of Part One is about why it is hard and how we solved it.

**We did not build it once and stop.** We built it, put it in a real room, watched real people use it, measured what went wrong, and fixed it. **Thirty-three times over.**

### What got better

| What | Before | After | What this means |
|---|---|---|---|
| **Sound being recorded** | 82% | **99.9%** | The computer was losing about one word in every five. Now it loses almost nothing. |
| **How clearly a voice stands out** | Barely above background noise | **4× clearer** | One setting inside the microphone was working against us. |
| **How far away you can stand** | Lean in close | **Across the room** | We stopped asking "is it loud?" and started asking "is it a voice?" |
| **Wait before she replies** | About 4.3 seconds | **About 3.3 seconds** | A full second faster, on every single question. |
| **Time for the AI to start** | 1.30 seconds | **0.66 seconds** | We stopped it thinking silently before speaking. |
| **Stopping her mid-sentence** | Talk for 0.42 seconds | **0.26 seconds** | She stops on your first word now. |
| **False wake-ups** | About once a minute | **None** | She used to wake herself from background noise. |
| **Cost per song played** | About 25 paid requests | **About 4** | Six times cheaper, and faster too. |

### Three things worth knowing

1. **Every number here was measured on the real device.** Not estimated, not copied from a spec sheet. Tested in the actual room, with the actual microphone.
2. **We include the things that failed.** Section 5 describes a feature we built properly, tested properly, found did not work, and removed.
3. **We know what is still not perfect.** Section 8 lists the remaining weak spots honestly.

---

## 2. Making her hear you

This section holds the biggest fix in the whole project. **Nobody could see it. It only appeared when we measured.**

### 2.1 One word in five was never recorded

> **The problem** — People said: *"I have to say everything twice."* Words went missing from the middle of sentences.
>
> **What we found** — We measured how much sound was actually saved, against how much was spoken.

| Setting | Sound actually recorded |
|---|---|
| What we were using | **82%** |
| A bigger setting | 62% |
| A bigger setting still | 57% |
| **Letting the system choose for itself** | **99.9%** |
| A standard tool, same microphone | 100% |

> **The key clue** — Making the setting *bigger* made it *worse*. That told us the fault was in how we were asking, not in the hardware. And the standard tool reaching 100% proved the microphone was never faulty.
>
> **The result — about a fifth of every recording the product had ever made was being thrown in the bin. That is now zero.** Every accuracy test we ran before this fix was against damaged sound.

### 2.2 The microphone was fighting us

> **The problem** — *"Close the test file"* was heard as *"Here is the closed test file."*
>
> **What we found** — Cheap microphones have a setting that turns the volume up when things go quiet. But between your words there *is* quiet — so it turns the volume up on the silence, then slams it down when your next word starts. **It flattens exactly the part of speech that carries the meaning.**
>
> **The result** — With it switched off, your voice comes through **nearly four times more clearly**, and the background noise got slightly quieter too. We now switch it off every time the device starts, because it turns itself back on after a reboot.

### 2.3 Why "loud enough" was the wrong question

> **The problem** — *"She only hears me if I lean into the microphone."*
>
> **What we found** — The old system worked like a volume switch. So we measured the room:

| | Range |
|---|---|
| Background noise | up to **1416** |
| A person talking at normal distance | **280 to 1600** |

> **These overlap almost completely.** Room noise can be louder than a person a few feet away. **So no volume setting works.** Set it high and she cannot hear anyone unless they lean in. Set it low and she thinks a fan is a person.
>
> **What we did** — We stopped asking *"is it loud?"* and started asking **"does this sound like a human voice?"** The new system reads the shape of the sound, thirty-three times a second.
>
> **The result — a quiet voice across the room is still obviously a voice. A loud fan is still obviously not.** This is the single change that lets someone talk to her from a normal distance.

### 2.4 The first word was being cut off

> **The problem** — *"Hey Liza, play the song Shape of You"* arrived as one word: **"The"**.
>
> **What we found** — The system started recording at the moment it decided somebody was speaking. By then, the sound that convinced it had already gone. **It was throwing away the very syllable that woke it up.**
>
> **What we did** — The microphone now records all the time into a small rolling memory, and reaches **0.7 seconds back in time** to pick up the beginning.
>
> **The result** — The first word survives. That was half of *"I have to say it twice."* The other half: her microphone used to be switched off for the whole time she was talking, so a quick reply landed while nothing was recording. **Nothing is switched off any more.**

---

## 3. Making her fast

**Waiting is what makes a device feel dead.** When you stop speaking, about **3.3 seconds** pass before Liza starts talking. Here is where all of it goes:

| Step | Time | Can we control it? |
|---|---|---|
| Working out that you have finished | **0.55 sec** | **Yes — the only part we control** |
| Turning your voice into text | 0.60 sec | No — internet round trip |
| The AI writing its first sentence | 0.90 sec | Partly — we choose the AI |
| Turning that sentence into a voice | 0.96 sec | No — internet round trip |
| The device itself | 0.29 sec | Small |

**Three of the five steps are trips over the internet.** We do not own that time, so all our work went into the one part we do.

| Version | Wait | Why it changed |
|---|---|---|
| First try | 1.50 sec | The original setting. Silence on every question. |
| Second try | 0.80 sec | Padding the old system needed. |
| **Now** | **0.55 sec** | The new system reads the difference directly, so most of that padding was dead air. |

**We removed about one second from every conversation.** A real person starts replying about 0.2 seconds after you stop.

### Choosing the AI, tested with real questions

We compared two AI services with **real tutoring questions in English, Hindi and mixed**, and — crucially — **with the full instructions we actually send.** Tested with short instructions they look identical. With the real ones they do not.

| AI service | To first word | Cost | Failures |
|---|---|---|---|
| **Google Gemini Flash — chosen** | **0.83 sec** | Higher | 0 of 6 |
| DeepSeek Flash | 2.41 sec | **10× cheaper** | 1 of 6 |

**We chose the faster one, and the reason is those 1.6 seconds.** That is silence in the room, on every question. The cheaper option is genuinely good, and **switching to it is a one-line change** already written into our settings if cost ever matters more than speed.

### Stopping the AI from thinking silently

Modern AI models often "think" privately before answering. **Nothing can happen while they do** — no sentence written means no speech, means silence.

| Setting | Time before the first word |
|---|---|
| **Thinking switched off** | **0.66 sec** |
| Left alone at the default | **1.30 sec** |

**Half a second of silence removed from every question.**

---

## 4. Making her understand

### 4.1 She invents speech out of silence

Handed a cough, a door or a fan, the speech system **never returns nothing.** It returns whatever hint we gave it, or a phrase from its training. We built four defences, cheapest first: don't make the call at all for anything too short or quiet; a list of the phrases it invents; matching families of phrases rather than exact words; and spotting when it loops one phrase over and over.

### 4.2 Teaching it school vocabulary

> **The problem** — From a real room: *"play a mitochondria"* came back as *"play a microcontroller"* — and it went looking for microcontroller videos.
>
> **The result** — We now include biology, chemistry, physics and maths terms in the hint, in both languages. **Hints are free — they cost nothing per request.**

### 4.3 English quietly turning into Spanish

> **The problem** — A plain English question came back answered **in Spanish.**
>
> **What we found** — The speech system recognises about 99 languages and picks one automatically. On a short or noisy recording it sometimes picks confidently and wrongly. **And it gets worse:** the wrong language goes into the answer, the answer goes into the conversation history, and the history then **teaches every following reply to do the same.** One bad moment poisoned the whole session.
>
> **What we did** — Anything that is not English or Hindi is immediately re-checked with the language locked.

### 4.4 Refusing to make things up

> **The problem** — Asked about a college it had never heard of, the AI **invented company names and competition wins that do not exist** — delivered confidently.
>
> **What we did** — The instructions no longer say "admit you don't know." They say **search the internet**, and state plainly that **inventing something plausible is the worst possible answer.** News always comes from a live search, never memory.

### 4.5 Answers that fit the question

> **The problem** — Every answer used a fixed three-part lecture structure. **Saying "Hello!" produced a lecture on the purpose of greetings.**
>
> **What we did** — The length now matches the question. Quick question, one sentence, stop. **This turns out to be our main cost control too — see Section 12.**

---

## 5. The feature we built and threw away

**We are including this in full because it shows how we work.**

### Our first assumption was wrong

The idea was: *when you talk over her, you are closer to the microphone than the speaker, so you will be louder.* **We measured it:**

| Situation | How loud it arrives |
|---|---|
| Speaker off — just your voice | up to **4365** |
| Speaker on — **her own voice** | **3535** |
| Speaker on — **your voice** | never above **2067** |

**Your voice arrives quieter than hers does.** The microphone sits near the speaker. When you both talk, the total rises by only about 16% — and her own voice jumps far more than that between syllables. **So no setting can work.**

### We built the standard fix, and it failed

There is a well-known technology for this: an **echo canceller** — what stops you hearing yourself on a video call. We built one, and tested it properly. **It never locked on at all.** The sound goes out through one USB device and comes back through another, each converting it at a different rate. An echo canceller needs the returning sound to match the original almost exactly. After that round trip, it does not.

> ### The trap we avoided
> Turning on one extra option showed a headline **10 dB improvement.** That looks like success.
>
> It is worthless. That option is a *noise reducer* — it turned **everything** down, including the person talking. We tested it properly and found **it quietened the person more than the echo.** The thing we needed got slightly *worse*.
>
> **Had we trusted the headline number, we would have shipped a step backwards and called it a feature.**

**What we shipped instead:** a simpler method that works reliably in the gaps between her sentences, plus honest documentation of what actually fixes it — turn the speaker down to about half, move the microphone away from it, or change the audio routing.

---

## 6. Music, video, and doing things

### 6.1 The video that closed itself after one second

The most revealing bug in the project. A user reported: *"it opens the file, but after 1–2 seconds it closes on its own."* It was a chain of three things:

1. The video starts. The system listening for someone talking over it takes its first reading from a moment **before any sound was playing** — so the video instantly looked like somebody shouting.
2. That triggered a check for the wake word, listening to the video's own soundtrack.
3. The speech system is given a hint of what to listen for — and that hint is literally **"Hey Liza."** Given a soundtrack it could not make sense of, **it handed our own hint straight back to us.** We read that as a wake word and stopped the video.

**Every single time.** Four separate fixes shipped, and the whole chain is written down so it cannot come back.

### 6.2 What people actually say

Every fix here came from a real recording of a real person.

| What was said | What went wrong |
|---|---|
| "Play" said in Hindi | The speech system writes it in Hindi letters, and **spells it differently each time.** The request fell through to the AI, which politely explained how to search YouTube |
| "**Okay,** can you play a video of gravity?" | Our patterns expected the sentence to *start* with "play". **The word "Okay" alone broke it** |
| "Play a video of gravity **in YouTube**" | It searched for the literal phrase *"gravity in YouTube"* |
| "Play heatwave" | Went to the AI, which happily said **"okay, playing Heatwave" — and nothing played** |

> **The principle we settled on:** "Play X" is handled **by our own code, before the AI is involved.** It guarantees it works, **and removes a whole trip to the internet before the music starts.** The instructions now also forbid her from ever claiming to play something: **saying so when nothing plays makes her a liar.**

### 6.3 Six times cheaper per song

Her microphone must be off while music plays, or she hears lyrics as commands. But that would leave "stop the music" as the one thing she could never hear. So she checks periodically — and **each check costs money.**

| Version | Checks | Cost per song |
|---|---|---|
| First | every 2 sec | **~25 requests** |
| **Now** | every 10 sec | **~4 requests** |

**Six times cheaper, no more audible dipping, and faster to respond than any earlier version** — because a raised voice is now detected instantly and costs nothing.

### 6.4 Safety, built into our own code

Liza can open your files, list folders, change the screen, and run commands. **The AI decides what you asked for. Our own code decides what is allowed** — because an instruction to an AI is a request, and this needs to be a guarantee.

- **Administrator commands refused outright**, and the AI is told it cannot override this
- **Every part of a command is checked**, because *"date; sudo reboot"* is two commands and only the second matters
- **Commands that destroy the device are blocked** — and one spelling slipped past an earlier version, which is exactly why this is tested rather than assumed
- **She cannot leave your home folder**

---

## 7. What Part One shows

1. **We measure instead of assuming.** The biggest fault in the project — a fifth of all sound thrown away — produced no error message, and no user ever described it accurately.
2. **We report our failures honestly.** The echo canceller was built, tested, and rejected on the evidence. **That discipline is what stops a good demo becoming an expensive support problem.**
3. **Every setting can be defended.** Not one number in the product is a guess. Each sits in the code beside the measurement that produced it.
4. **Real users found real problems, and the causes ran deeper than the symptoms.** No amount of automated testing would have found them.

---

## 8. What still isn't perfect

| Limitation | What would fix it |
|---|---|
| **Interrupting her works in the gaps between sentences, not mid-sentence** | A different audio setup that keeps both sides in sync. The software already exists on the device |
| Turning the speaker above 80% makes interrupting harder | The same fix, or a headset-style microphone |
| Speech, thinking and voice all need internet | On-device AI is improving fast. We already shipped a local voice once and replaced it on quality |
| Speaking very quietly over loud music can take 10 seconds to register | Making it more sensitive makes the music dip constantly. We measured it — not worth it |

---

# PART TWO — WHERE THIS GOES

---

## 9. Where we are today, and what has to change

The device works. But **it was built to be one device in one room**, and some of it cannot be sold to a thousand homes as it is.

| What it is now | Why that stops us growing |
|---|---|
| **The secret keys are stored on the device** | Every device would carry our billing keys. Anyone could take them and spend our money. |
| The conversation is saved in a file on the device | Nothing syncs. Change the device and the history is gone. We cannot show a parent any progress. |
| Settings are edited by hand on the device | To change how she behaves, someone must physically connect to that device. |
| **There is no way to see how a device is doing** | Our biggest bug — a fifth of all sound lost — was silent. If that happened on 500 devices tomorrow, **we would find out from complaints, not data.** |
| Everything is one program | The part that listens and the part that decides what to say are tangled together. |

**None of this is a rewrite.** It is a reorganisation, and Section 15 gives the order.

> ### The one rule that shapes the whole plan
> **Physics stays on the device. Decisions move to the server.**
>
> How loud the room is, where the microphone is, how to tell a voice from a fan — those are about *that room and that microphone*, and stay local. Everything else — what Liza knows, how she answers a Class 6 versus a Class 11 student, what she is allowed to do — **belongs on our server, where we change it for everyone at once.**

---

## 10. The plan in one picture

```
   ┌──────────────────────┐   ┌──────────────────────┐   ┌────────────────────┐
   │   STUDENT'S APP      │   │   PHYSICAL DEVICE    │   │   ADMIN CONSOLE    │
   │  Pick your class     │   │  Hears the room      │   │  Manage students   │
   │  Talk / practise     │   │  Speaks the answer   │   │  Set the syllabus  │
   │  See your progress   │   │  Plays media         │   │  Watch the devices │
   └──────────┬───────────┘   └──────────┬───────────┘   └─────────┬──────────┘
              └──────────────┬───────────┴─────────────────────────┘
                   ┌─────────▼──────────┐
                   │    OUR SERVER      │
                   │  Who is this?      │  ← accounts and login
                   │  What class?       │  ← builds the right instructions
                   │  Ask the AI        │  ← we hold the keys, not the device
                   │  Save the progress │  ← history, mistakes, topics
                   │  Count the cost    │  ← what each student costs us
                   └────────────────────┘
```

**The important change:** today the device talks straight to the AI companies. In the new plan, **everything goes through our server first.** That one move is what makes accounts, class levels, progress reports, cost control and safety possible at all.

**It costs us a fraction of a second.** Part One made latency the headline, so we take it seriously: servers in the same region as our users, and the answer passed straight through as it arrives. **Budget: under 0.15 seconds added**, measured on every release.

---

## 11. The main new feature: Liza knows what class you're in

A student picks **Class 8, CBSE, Hindi medium** once, and everything changes.

| What changes | Class 4 | Class 11 |
|---|---|---|
| **How deep the answer goes** | "Plants make their food from sunlight." | "Light reactions produce ATP and NADPH, which the Calvin cycle then uses." |
| The words she expects to hear | Simple science words | Organic chemistry, calculus |
| The examples she reaches for | Things around the house | Things in an exam paper |
| How long she talks | Two short sentences | A full explanation with an example |
| How she marks you | Encouraging, one correction | Exam-style, marks the specific error |

### Why this is easier than it sounds

**The device is already built this way.** Three pieces are already swapped in and out on every request, and the class level slots into the same places:

1. **The vocabulary hint** we give the speech system. That is *why* "mitochondria" is no longer heard as "microcontroller." **A Class 11 chemistry list and a Class 4 list are just different lists.**
2. **The teaching instructions**, already swapped between Ask, Practise and Test modes. **A class level is one more thing to swap.**
3. **The order of the instructions.** A student's class almost never changes, so it sits high up where it keeps the AI discount. **The feature costs us almost nothing per request.**

---

## 12. The app — what a student sees and does

**Setting up:** sign in → pick class (1–12) → pick board (CBSE, ICSE, state) → pick language → pick subjects → optionally pair a physical device.

| Screen | What they see | What they can do |
|---|---|---|
| **Home** | Talk button, recent topics, their streak | Start talking, resume a topic |
| **Talk** | Liza's face, live text of the conversation | Ask anything, interrupt her, switch mode |
| **Modes** | Ask me · Practise with me · Test me | Switch any time — *these already exist* |
| **My progress** | Topics covered, repeated mistakes, time spent | Tap a topic to revise it |
| **History** | Every past conversation, searchable | Replay an answer, delete a conversation |
| **Settings** | Language, voice speed, access | Change language and voice, **delete all my data** |

**They can change:** language and voice, which subjects appear, their own history, and pausing the microphone.
**They cannot change:** their class level if a parent or school locked it, the safety rules, or anything belonging to another student.

> ### Where progress comes from — the interesting part
> **Test me mode already produces exactly the right data, and today we throw it away.**
>
> When a student explains a topic from memory, Liza already gives a structured verdict: what they got right, up to three specific mistakes, and one topic to revise. **Today that is spoken aloud and forgotten.**
>
> Save each verdict and after a few weeks you have: which topics they covered, which mistakes they repeat, which subject is weakest, and a weekly summary a parent reads in thirty seconds.
>
> **We do not have to build a testing system. We have to start saving the one we already built.**

---

## 13. The console — what an admin does

**A school admin can:** add and remove students, set each one's class and board, lock a class level, upload the school's own notes and papers, see every student's progress, review anything flagged, and see which devices are working.

**Our team can also:**

| Area | What we can do | Why it matters |
|---|---|---|
| **Content library** | Edit the syllabus, vocabulary and teaching instructions for every class | Improve every student at once, nothing to install |
| **Device health** | See each device's microphone health and last contact | **The direct answer to our worst-ever bug.** A device losing sound now raises an alert instead of a complaint |
| **Remote calibration** | Recalibrate a microphone without visiting it | Rooms change. Microphones get moved. |
| **Safety review** | Read flagged conversations, publish a rule change immediately | A children's product needs this to be fast |
| **Model routing** | Choose which AI serves which customer tier | We already tested one **ten times cheaper** |
| **Release control** | Roll out to 5% of devices, watch, then everyone | Today a bad change reaches everybody at once |

**What nobody can do:** read a conversation without it being logged, switch off the safety rules, or listen to raw voice recordings.

---

## 14. What lives where

| Layer | What it holds |
|---|---|
| **The device** | Listening, telling a voice from noise, the measured microphone settings, the wake word, the speaker, the screen and animations, and an offline message if the internet drops |
| **The app** | Login, class and language choices, the talk screen, progress, history, settings |
| **Server — front end** | The student web app, the admin console, the parent's weekly view |
| **Server — back end** | Accounts · conversation service · **provider routing (holds all the keys)** · progress · content · safety · device health · usage metering |
| **Server — storage** | Main database, uploaded school material, a cache of common answers, and background jobs for reports and alerts |
| **Outside** | The three AI services we buy — all behind our routing, **so any one can be swapped without touching the app or the device** |

---

## 15. Keeping children safe

Our users are children, in India, and the law here has real teeth.

| Rule we build to | How we do it |
|---|---|
| **A parent or school must consent** | Consent is part of sign-up, and is recorded |
| **Voice recordings are not kept** | The audio produces text and is deleted immediately. **The text is what we store**, and only if the account allows it |
| A student can delete everything | One button, and it actually deletes |
| We do not sell data or train on it | Written into the provider contracts |
| Every access is logged | Any admin looking at a student's data leaves a record |
| Data stays in India | Servers in-region, which also helps speed |
| Refusals are visible | Everything she declined is reviewable by the school |

**The safety rules from Part One carry forward** — and they are enforced in our own code, not by asking the AI nicely. That was a deliberate decision from the start, and it is exactly what lets us stand behind it now.

---

## 16. The order we build it in

**Phase 1 — Make it sellable.** *Nothing a user sees changes. Everything about how we run it changes.* Split the program into a device part and a service part, move all keys onto our server, add accounts, add device health reporting, add staged rollouts.
> **Why first:** everything after depends on it — and shipping the app without it means shipping our billing keys to strangers.

**Phase 2 — The app, and the class level.** Android and web app, class and board selection, the syllabus map, conversations syncing between app and device.
> **What this unlocks:** we can sell to a student who does not own the hardware. **The app becomes the product; the device becomes the premium version.**

**Phase 3 — Progress.** Save the marks Test me mode already produces, build the progress screen and the weekly parent summary.
> **Why this matters commercially:** a parent will not pay monthly for a talking toy. **They will pay for evidence their child is learning.**

**Phase 4 — Schools.** The admin console, teacher accounts, exportable reports, schools uploading their own material.
> **What this unlocks:** one sale becomes forty students instead of one.

**Phase 5 — Running it cheaply at scale.** Cache common answers, route customer tiers to different AI services, regional servers, and renegotiate the voice contract.

---

## 17. What it costs us to run

> **These are the published rates as of August 2026, checked against each provider. The only estimate is how much a student uses the product.**

| Service | What we pay |
|---|---|
| **Speech-to-text** | **$0.111 per hour** of audio — billed with a **10-second minimum per request** |
| **The AI brain** | **$0.30 per million words in**, **$2.50 per million words out** |
| **The voice** | **1 credit per character** — about **$37 per million characters** on the $299 plan, falling to about **$5 per million** at volume |

### What one question costs

| Service | Cost | Share |
|---|---|---|
| Speech-to-text | $0.00031 | 3% |
| The AI brain | $0.00065 | 6% |
| **The voice** | **$0.00935** | **91%** |
| **Total** | **$0.0103** | |

> ### The single most important number in this report
> **The voice is 91% of what we pay.** The AI brain — the part everyone assumes is expensive — is 6%.
>
> **Every serious cost decision we make is a decision about the voice.**

**One detail that costs us double:** our recordings are about 5 seconds, but speech-to-text is billed with a 10-second minimum. **We pay for twice what we use, on every question.** This is exactly why the music fix in Section 6.3 matters — cutting from 25 checks per song to 4 saved 21 billed requests every time.

### Cost per student, per month

| Usage | Questions/month | At list price | At volume pricing |
|---|---|---|---|
| Light | 50 | $0.52 | $0.11 |
| **Typical** — 8 a day | 200 | **$2.06** | **$0.44** |
| Heavy — 20 a day | 500 | $5.15 | $1.11 |

**That right-hand column is the business.** A volume voice contract takes a typical student from **$2.06 to $0.44 a month** — a 79% cut in running costs, from one negotiation.

### The levers, in the order we would pull them

1. **A volume voice contract** — up to 79% of total cost. A commercial negotiation, not an engineering project.
2. **Shorter answers** — **already done**, in Section 4.5. A one-sentence answer costs a fifth of a four-sentence one. **That fix was made for user experience; it turns out to be our main cost control.**
3. **Cache common answers** — thousands of students in the same class ask "what is photosynthesis". We should pay once.
4. **Cheaper AI for free users** — already tested, one line to switch.

---

## 18. What we charge

> **Rupee prices assume about ₹88 to the dollar.**

| | **Free** | **Plus** | **Family** | **School** |
|---|---|---|---|---|
| **Price** | ₹0 | **₹299/month**<br>or ₹2,999/year | **₹499/month**<br>or ₹4,999/year | **₹1,200 per student<br>per year** |
| Questions | 10 a day | Unlimited* | Unlimited* | Unlimited* |
| Ask me | ✓ | ✓ | ✓ | ✓ |
| Practise with me | — | ✓ | ✓ | ✓ |
| Test me + marking | — | ✓ | ✓ | ✓ |
| **Video library** | 3 a day | **Full access** | **Full access** | **Full access** |
| **Study material** | — | **Full access** | **Full access** | **Full + school's own** |
| Progress reports | — | ✓ | ✓ per child | ✓ per class |
| Weekly parent summary | — | ✓ | ✓ | Teacher reports |
| Offline downloads | — | ✓ | ✓ | ✓ |
| Answer speed | Standard | **Priority** | **Priority** | **Priority** |
| History kept | 7 days | Forever | Forever | Per school policy |
| Device pairing | — | ✓ | ✓ | ✓ |
| Admin console | — | — | Parent view | **Full console** |

<small>*Fair use applies — capped at 40 questions a day.</small>

**Hardware:** the Liza device at **₹9,999 one-time**, with **12 months of Plus included.**

### What a subscription actually unlocks

- **The video library.** Free gets three a day from a general search. Subscribers get the **curated, syllabus-mapped library** — every video tied to a topic in their class and board, ad-free, safe to leave a child alone with. **This is the clearest reason to pay.**
- **Study material.** Notes, worksheets, solved examples and past papers for their exact class and board, downloadable. Liza teaches directly from them — *"open my chapter 6 notes and test me on it."*
- **The two study modes.** Free gives Ask me, which is the demo. **Practise and Test me are the product** — they turn a question box into something used every evening, and they are where progress data comes from.
- **Progress and the weekly parent summary.** **This is what renews the subscription** — not the talking, the evidence.
- **Priority speed.** Subscribers route to the faster AI; free users to the cheaper one, 1.6 seconds slower to start. A real difference, at no cost to us.

### Does the pricing work?

At a typical 8 questions a day:

| Plan | Revenue/month | At list price | At volume pricing |
|---|---|---|---|
| **Plus ₹299** | $3.40 | $2.06 → **39% margin** | $0.44 → **87% margin** |
| **Family ₹499** (2 children) | $5.67 | $4.12 → **27% margin** | $0.88 → **84% margin** |
| **School ₹1,200/yr** | $1.14 | $2.06 → **loses money** | $0.44 → **61% margin** |

> ### The honest conclusion
> **At list price, Plus works and School does not.** The school tier — the one that turns a single sale into forty students — **only becomes profitable once we have the volume voice contract.**
>
> That is not a reason to change the plan. It is a reason to know the order: **sign the voice contract before signing the schools.**

**Free users cost us almost nothing** — ten questions a day on the cheaper AI keeps them at about **$0.11 a month**, cheap enough to give away freely, which is what conversion in the Indian education market depends on.

---

## 19. What could go wrong

| Risk | How serious | What we do about it |
|---|---|---|
| **The voice contract does not improve** | **Highest** | Everything in Sections 17 and 18 turns on it. Fallbacks: shorter answers, caching, a second provider quoted against the first. **We have already swapped the voice once, so we know it is a two-week job** |
| An AI provider raises prices or shuts down | High | Everything goes through our routing layer. Swapping is a settings change |
| A child asks something we should have refused | Very high | Rules in our own code, full review log, a rule change reaching everyone in minutes |
| Devices fail quietly in the field | High | Health reporting from Phase 1. **Our worst bug was invisible once — never again** |
| Free users cost more than expected | Medium | Caps enforced on our server, not in the app, so they cannot be bypassed |
| Features creep and the 3.3-second wait grows | Medium | Latency measured and reported on every release |

---

## 20. The whole thing, in ten lines

1. We built a talking study assistant on **cheap hardware**, in **English and Hindi**, answering in about **three seconds**.
2. We tested it **thirty-three times on the real device**, and every number in it can be defended.
3. The biggest fix — **a fifth of all sound was being thrown away** — was invisible until we measured it.
4. We built an echo canceller, proved it did not work, and **removed it rather than ship a headline number.**
5. **What remains imperfect is written down**, with the fix for each.
6. Next is **an app where a student picks their class**, and Liza teaches at that level.
7. That feature **fits the existing design** — three things are already swapped on every request, and a class level is one more.
8. **Progress reports are already half built** — Test me mode marks students properly today; we just have to save it.
9. We charge **₹299 a month**, unlocking the full video library, study material, the two study modes, and the progress reports that make a parent renew.
10. **The voice is 91% of our costs.** The highest-value action available to this company is a volume contract with the voice provider — it takes a typical student from **$2.06 a month to $0.44**, and makes the school tier profitable.

---

*Every figure in Part One was measured on the actual device, in the actual room, with the actual microphone. Every rate in Part Two is the provider's published price as of August 2026.*
