# Liza — What We Tested, and What Changed

**A talking assistant that runs on a small, low-cost computer**
Written in plain language. No technical background needed.

| | |
|---|---|
| **What it is** | Liza — a small screen you talk to. She answers questions, teaches, plays music and videos, and can open files for you. |
| **Languages** | English, Hindi, and both mixed together in one sentence |
| **Hardware** | A Raspberry Pi (a computer the size of a deck of cards) · a 5-inch touchscreen · a cheap USB microphone and speaker |
| **Date** | 20 August 2026 |
| **Size of the work** | One program, 7,023 lines · 33 rounds of changes |

---

## 1. The short version

We set out to build a full talking assistant on **cheap hardware**. Not a phone. Not a laptop. A tiny computer, a £5 microphone, and a small speaker.

That is hard, and most of this document is about *why* it is hard and *how we solved it*.

**We did not build it once and stop.** We built it, put it in a real room, listened to real people use it, measured what went wrong, and fixed it. Thirty-three times.

### What got better

| What | Before | After | What this means |
|---|---|---|---|
| **Sound being recorded** | 82% | **99.9%** | The computer was losing about 1 word in every 5. Now it loses almost nothing. |
| **How clearly we hear a voice** | Voice barely louder than background noise | **4× clearer** | We turned off a setting in the microphone that was working against us. |
| **How far away you can stand** | You had to lean in close | **Normal distance** | You can now talk to her from across the room. |
| **Wait before she replies** | About 4.3 seconds | **About 3.3 seconds** | A full second faster, on every single question. |
| **Time for her brain to start** | 1.30 seconds | **0.66 seconds** | We stopped the AI from "thinking silently" before speaking. |
| **Stopping her mid-sentence** | You had to talk for 0.42 seconds | **0.26 seconds** | She now stops when you say your first word. |
| **False wake-ups** | About once a minute in an empty room | **None** | She used to wake herself up from background noise. |
| **Cost per song played** | About 25 paid requests | **About 4** | Six times cheaper, and faster too. |

### Three things worth knowing

1. **Every number here was measured on the real device.** Not estimated. Not copied from a spec sheet. We tested it in the actual room, with the actual microphone.

2. **We include the things that failed.** Section 6 describes a feature we built, tested properly, found did not work, and threw away. Finding that out in our own lab is much cheaper than finding it out in a customer's living room.

3. **We know what is still not perfect.** Section 12 lists the remaining weak spots honestly, and says what it would take to fix each one.

---

## 2. How to read this

Every problem in this document is written the same way:

> **The problem** — what went wrong, usually in the words of the person who reported it
> **What we found** — what we discovered when we measured it
> **What we did** — the fix
> **The result** — what the user gets now

---

## 3. How Liza works, in one picture

```
    You speak
        ↓
    Microphone picks it up
        ↓
    Is this a voice, or just noise?        ← Section 4
        ↓
    Turn the sound into words               ← Section 5
        ↓
    Which language was that?                ← Section 8
        ↓
    Think of an answer                      ← Section 7
        ↓
    Turn the answer into speech
        ↓
    Speaker plays it — while she is still
    writing the rest of the answer
```

**The last line matters.** She starts speaking her first sentence while she is still working out the second one. That is why she feels quick — you are never waiting for the whole answer to be finished.

---

## 4. How long you wait, and why

This is the most important measurement in the whole document. **Waiting is what makes a device feel dead.**

When you stop speaking, about **3.3 seconds** pass before she starts talking. Here is where every bit of that goes:

| Step | Time | Can we control it? |
|---|---|---|
| Working out that you have finished speaking | **0.55 sec** | **Yes — this is the only part we control** |
| Sending your voice away to be turned into words | 0.60 sec | No — internet round trip |
| The AI writing its first sentence | 0.90 sec | Partly — we choose which AI |
| Turning that sentence into a voice | 0.96 sec | No — internet round trip |
| The device itself | 0.29 sec | Small |

**Three of those five steps are trips over the internet.** We do not own that time. So all our work went into the one part we do own.

### The part we control

| Version | How long she waits | Why it changed |
|---|---|---|
| First try | 1.50 sec | The original setting. Silence on every single question. |
| Second try | 0.80 sec | We had to keep this much padding, because the old system could not tell a quiet word from a pause. |
| **Now** | **0.55 sec** | The new system can tell the difference, so most of that padding was just dead air. |
| Exam mode | 1.60 sec | Deliberately patient. A student reciting from memory pauses to think. |

**We removed about 1 second from every single conversation.** For comparison, a real person starts replying about 0.2 seconds after you stop.

---

## 5. Hearing you properly

This section has the biggest single fix in the whole project. **Nobody could see it. It only showed up when we measured.**

### 5.1 One word in five was never recorded

> **The problem** — People said: *"I have to say everything twice."* Words went missing from the middle of sentences.
>
> **What we found** — We measured how much sound was actually being saved, versus how much was spoken. We tried four different settings.

| Setting | Sound actually recorded |
|---|---|
| What we were using | **82%** |
| A bigger setting | 62% |
| A bigger setting still | 57% |
| **Letting the system choose for itself** | **99.9%** |
| A standard Linux recording tool, same microphone | 100% |

> **The key insight** — Making the setting *bigger* made it *worse*. That is the clue that told us the problem was in how we were asking, not in the hardware. And the standard tool getting 100% proved the microphone was never faulty.
>
> **What we did** — Stopped forcing a setting. Let the audio system pick its own.
>
> **The result — about a fifth of every recording the product had ever made was being thrown in the bin. That is now zero.** Every accuracy test we ran before this fix was against damaged sound.

### 5.2 The microphone was fighting us

> **The problem** — *"Close the test file"* was being heard as *"Here is the closed test file."*
>
> **What we found** — Cheap microphones have a setting called Auto Gain Control. It tries to be helpful: it turns the volume up when things go quiet. But between your words there *is* quiet — so it turns the volume up on the silence, then slams it down when you start the next word. **It flattens exactly the part of speech that matters.**
>
> We measured the same sentence, in the same room, with it on and off:

| | Setting on | Setting off |
|---|---|---|
| How loud your voice comes through | 653 | **1599** |
| Peak of your voice | 1605 | **2593** |
| Background noise | 457 | **392** |

> **The result — your voice comes through nearly 4 times more clearly, and the background noise got slightly quieter too.** All from switching off one setting. We now switch it off every time the device starts, because it turns itself back on after a reboot.

### 5.3 Why "loud enough" was the wrong question

> **The problem** — *"She only hears me if I lean into the microphone."*
>
> **What we found** — The old system worked like a volume switch: *if the sound is louder than X, it must be someone talking.* So we measured what the room actually sounds like:

| | Range |
|---|---|
| Background noise in the room | up to **1416** |
| A person talking at normal distance | **280** to 1600 |

> **These overlap almost completely.** Room noise can be louder than a person speaking from a few feet away. So there is **no volume setting that works**. Set it high and she cannot hear anyone unless they lean in. Set it low and she thinks a fan is a person.
>
> **What we did** — We stopped asking "is it loud?" and started asking **"does this sound like a human voice?"** The new system looks at the *shape* of the sound — the pattern a human voice makes — 33 times a second.
>
> **The result** — A quiet voice across the room is still obviously a voice. A loud fan is still obviously not. **This is the single change that lets someone talk to her from a normal distance.**

### 5.4 The first word was being cut off

> **The problem** — *"Hey Liza, play the song Shape of You"* arrived as one word: **"The"**.
>
> **What we found** — The old system started recording at the moment it decided someone was speaking. But by then, the sound that convinced it was already gone. **It was throwing away the very syllable that woke it up.**
>
> **What we did** — Now the microphone records all the time into a small rolling memory. When it decides you are speaking, it reaches **0.7 seconds back in time** and picks up the beginning too.
>
> **The result** — The first word survives. This was half of *"I have to say it twice."* The other half was that her microphone used to be switched off for the whole time she was talking, so if you replied quickly, nothing was recording. **Nothing is switched off any more.**

### 5.5 The setting that drifted away on its own

> **The problem** — She woke up for no reason. She sent silence off to be transcribed, which costs money and returns nonsense.
>
> **What we found** — The volume threshold recalculated itself constantly while waiting. In a quiet room it kept lowering itself:

| After waiting | Threshold | Actual room noise |
|---|---|---|
| At the start | 1000 | 300–500 |
| 1 second | 586 | 300–500 |
| 4 seconds | **370** | **300–500** |

> After a few seconds of quiet, **the threshold had sunk below the room itself**. Everything looked like speech.
>
> **What we did** — Built a version that physically cannot drift outside the range we measured for the room.

### 5.6 Two microphones with the same name

Both USB devices report themselves as *"USB PnP Sound Device"* — the same name. So the software picked whichever appeared first, which turned out to be **an empty microphone socket on the speaker adapter**, not the real microphone.

The same mistake on the speaker side was worse: pointing the sound *output* at a device that can only *record* makes Liza **completely silent**. We now name the exact devices, and shipped two small tools so anyone can check which is which.

---

## 6. Talking over her — the honest failure

**We are including this section in full because it shows how we work.** We built an attractive feature, tested it properly, found the data said no, and removed it.

### 6.1 Our first assumption was simply wrong

The idea was: *when you talk over her, you are closer to the microphone than the speaker is, so you will be louder. We can detect that.*

**We measured it.** Someone talking over her at normal volume, speaker at 80%:

| Situation | How loud it arrives at the microphone |
|---|---|
| Speaker off, just your voice | up to **4365** |
| Speaker on — **her own voice** | **3535** |
| Speaker on — **your voice** | never above **2067** |

**Your voice arrives quieter than hers does.** The microphone is close to the speaker, so her own voice comes back into it louder than you do. When you both talk, the total only goes up by about **16%** — and her own voice naturally jumps up and down by far more than that between syllables.

**So there is no setting that works.** Too high and nothing you say ever registers. Low enough to register, and she interrupts herself.

### 6.2 We built the standard fix, and it failed

There is a well-known technology for this problem: an **echo canceller**. It is what stops you hearing yourself on a video call. We built one in, using her own voice as the reference, and tested it properly.

| Test | Result | What it means |
|---|---|---|
| Best result across a full sweep of timing offsets | −2.5 dB | Weak |
| **Result at every other offset in the sweep** | **−2.5 dB** | **Completely flat — it never locked on at all** |
| Checking whether the timing was the problem | Steady, no drift | **It was not a timing problem** |

**Why it failed:** the sound goes out through one USB device and comes back through a different one, and each converts it at a different rate along the way. The echo canceller needs the returning sound to match the original almost exactly. After that round trip it does not, and no amount of tuning changes that.

> ### The trap we avoided
> Turning on one extra option showed a headline **10 dB improvement**. That looks like success.
>
> It is worthless. That option is a *noise reducer* — it was turning **everything** down, including the person talking. We tested it properly and found **it quietened the person more than it quietened the echo**. The thing we actually needed got slightly *worse*.
>
> **Had we trusted the headline number, we would have shipped a step backwards and called it a feature.**

**What we shipped instead:** a simpler method that works reliably in the gaps between her sentences, plus honest documentation of what actually fixes it — turn the speaker down to about half (her echo drops to roughly a third, and then you *are* the loudest thing in the room), move the microphone away from the speaker, or use a different audio setup.

### 6.3 Making interruption feel natural

For talking over **music**, we measured how much louder a voice must be than the track. We played a real song in an empty room and counted how often the system falsely thought someone had spoken, over 45 seconds:

| Setting | False triggers in 45 seconds | Effect |
|---|---|---|
| 1.15 | **4** — one every 11 seconds | The music kept dipping in volume the whole way through |
| **1.25 — what we use** | **1** | Nothing further is gained above this point |
| 1.35 — what we used before | 1 | **Too high to ever reach**, so every "Hey Liza, stop" took the slow route |
| 1.50 | 1 | A real voice becomes even harder to hear |

We also cut the time you must speak to stop her from **0.42 seconds to 0.26 seconds**. Nearly half a second is a whole word plus a pause — long enough that she finished her sentence before noticing. A quarter of a second is roughly one syllable. **She now stops while you are still on your first word.**

---

## 7. The brain

### 7.1 Choosing the AI — tested with the real questions

We compared two AI services using **real tutoring questions in English, Hindi and mixed** — six of each — and, crucially, **with the real full instructions we actually send.** That matters: a test with short instructions makes them look nearly identical. With the real ones, they are not.

| AI service | Time to first word | Total time | Cost | Failures |
|---|---|---|---|---|
| **Google Gemini Flash — chosen** | **0.83 sec** | **1.17 sec** | Higher | 0 out of 6 |
| DeepSeek Flash | 2.41 sec | 4.29 sec | About 10× cheaper | **1 out of 6** |

**We chose the faster one, and the reason is those 1.6 seconds.** That is silence, in the room, on every single question. It is the difference between talking *to* something and waiting *for* it.

The cheaper option is genuinely good and about ten times less expensive. **Switching to it is a one-line change**, already written and commented in our settings, if cost ever matters more than speed.

### 7.2 Stopping the AI from thinking silently

Modern AI models often "think" privately before answering. That thinking is invisible — but **nothing can happen while it does it.** No sentence written means no speech, means silence in the room.

| Setting | Time before the first word |
|---|---|
| **Thinking switched off — what we use** | **0.66 sec** |
| Thinking budget set to zero | 0.79 sec |
| Thinking set to minimum | 1.02 sec |
| Left alone at the default | **1.30 sec** |

**Half a second of silence removed from every question.** These are questions a knowledgeable person answers without pausing to think. We can turn thinking back on with one setting if something ever genuinely needs it.

### 7.3 A cost saving hidden in the order of a document

We send Liza's instructions to the AI with every single question. Those instructions are long — about **90% of everything we send** is the instructions, not the actual question.

AI services offer a discount: if the *beginning* of your message is identical to last time, they charge about half price for that part and answer faster. **But it only counts up to the first character that differs.**

**Our instructions had the current time near the top.** The time changes every minute. So the discount never applied — not once, ever.

**What we did:** moved the clock to the very bottom, and put everything that never changes at the top.

**The result — we now get the discount on most requests: roughly half price on the bulk of what we send, and a faster reply as well.** Nothing about the product changed. We just reordered a document.

### 7.4 Hindi was being cut off mid-word

Hindi text costs an AI about **three times** as much space as the same sentence in English. Our limit was set for English, so Hindi answers were being cut off in the middle of a word. We raised it, then set the length in the instructions instead, and brought the limit back down to a simple safety catch.

### 7.5 Answers that fit the question

> **The problem** — Every answer used a fixed three-part lecture structure. **Saying "Hello!" produced a lecture on the purpose of greetings.** Asking to convert a measurement produced four sentences before giving the number.
>
> **What we did** — The length now matches the question. Quick question, one sentence, stop. Only "how does this work" or "why does this happen" gets a full explanation.
>
> **A second bug we found while checking the first** — the code stripped a greeting off the front of every answer. So a reply that was *only* "Hello!" became empty, and **was played as silence.** Nobody had noticed, because greetings had always been followed by a lecture.

### 7.6 Refusing to make things up

> **The problem** — Asked about a college it had never heard of, the AI **invented company names and competition wins that do not exist.** Delivered confidently.
>
> **What we did** — The instructions no longer say "admit you don't know." They say **search the internet.** They list exactly which kinds of question require a live search, and state plainly that **inventing something plausible is the worst possible answer.**
>
> News always comes from a live search, never from memory — because what an AI remembers is months out of date, and *a stale headline delivered confidently is a wrong answer wearing a right answer's clothes.*

---

## 8. Two languages at once

### 8.1 The voice

We started with a free voice that runs on the device itself. It was fast, but robotic, and in practice English-only. We replaced it with a cloud voice where **one voice speaks both languages**, switching automatically sentence by sentence.

### 8.2 English quietly turning into Spanish

> **The problem** — A plain English question came back answered **in Spanish.**
>
> **What we found** — The speech system can recognise about 99 languages, and it picks one automatically. On a short or noisy recording it sometimes picks confidently and wrongly.
>
> **And it does not stop there.** The wrong language goes into the answer. The answer goes into the conversation history. The history then **teaches every following reply** to do the same — overruling our instruction to reply in English. One bad moment poisoned the rest of the session.
>
> **What we did** — Anything that is not English or Hindi is immediately re-checked with the language locked. Caught on the turn it happens.

### 8.3 Hindi text breaks standard programming tools

Three separate bugs, all from the same root cause: **standard text tools are built for English letters, and Hindi script does not work that way.**

| What broke | What the user saw | Fix |
|---|---|---|
| Splitting a sentence into words | Hindi sentences were shredded into meaningless fragments, which then matched *other* Hindi sentences — so **every Hindi sentence looked like a repeat of the last one** | Taught the tool the Hindi alphabet |
| Finding the end of a word | Some Hindi words matched, others left stray marks behind | Split on spaces instead |
| Invisible characters | Recordings containing nothing but invisible formatting marks were **treated as real questions and answered** | Stripped out and treated as silence |

### 8.4 The feature that ate the student's answer

> **The problem** — In exam mode, where a student explains a topic and Liza marks them, **parts of what the student said were silently disappearing** before reaching the marking. A grade based on half an answer, with nothing on screen to say so.
>
> **What we found** — We had a check to spot Liza's own voice coming back through the microphone: *does what we just heard match what she just said?* But it counted common words like "is", "the", "and", "में", "है". **Every sentence contains those.** A normal sentence — "it is in the leaf and it uses light" — scored 5 out of 8 on common words alone and was thrown away as an echo.
>
> **What we did** — Only meaningful words count now, and short replies must match completely before being discarded.

---

## 9. Music and video

### 9.1 Three attempts to get playback working

| Version | What we tried | Why we moved on |
|---|---|---|
| First | A basic music player already on the device | It only played MP3 files. YouTube did not work at all. |
| Second | We wrote our own player, decoding the video ourselves and drawing it on screen | The device had no media player of any kind, so we had no choice. It worked — 30 frames a second — but only by requesting one specific older video format, because the modern ones need more processing power than this device has. |
| **Now** | A proper media player, fed directly by a downloader | The player's own downloading kept failing on YouTube's links. Feeding it the data ourselves is reliable. |

### 9.2 Getting a result that actually plays

The old code simply took the first search result. **Tested in the real world: the top result for a popular devotional song is regularly a 24-hour live stream** — which never starts playing. So are region-blocked videos, and videos with no sound track.

Now it collects several results, ranks them by how well the title matches what you asked for, rejects live streams and silent videos, and **moves on to the next one instead of giving up.**

### 9.3 Two things could not use the speaker at once

The speaker was being claimed exclusively. So whichever started second — her voice, or the music — **failed silently and died.** We switched to a shared audio setup. **Now she can talk over the music, and more importantly she can still hear and answer "stop it."**

### 9.4 What people actually say

Every fix here came from a real recording of a real person.

| What was said | What went wrong | Fix |
|---|---|---|
| "Play" said in Hindi | The speech system writes it in Hindi letters — and **spells it differently each time.** Neither spelling matched, so the request went to the AI, which politely explained how to search YouTube | We accept the spellings it actually produces |
| **"Okay,** can you play a video of gravity?" | Our patterns expected the sentence to *start* with "play". **The word "Okay" alone broke it** | Polite openings are now removed before matching |
| "Play a video of gravity **in YouTube**" | It searched for the literal phrase *"gravity in YouTube"* | "In YouTube" is now stripped out |
| "Play Tum Hi Ho by Arijit Singh" | Came out as *"Pili kong hihobay arijit singh"*. **No pattern can recover that** | The AI can, so it cleans up mangled titles |
| "Play a **guessing game**" | Got hijacked into a music search | We keep a list of things that are not media |
| "Play heatwave" | Went to the AI, which happily said **"okay, playing Heatwave" — and nothing played** | Now handled directly. The instructions also forbid ever claiming to play something: **saying so when nothing plays makes her a liar** |

> **The principle we settled on:** "Play X" is handled **by our own code, before the AI is ever involved.** It is a straightforward command, not something to reason about. Doing it ourselves guarantees it works, **and removes a whole trip to the internet before the music starts.**

### 9.5 The video that closed itself after one second

The most revealing bug in the project. A user reported: *"it opens the file, but after 1–2 seconds it closes on its own."*

It was a chain of three things:

1. The video starts. The system that listens for someone talking over the music takes its first reading from a moment **before any sound was playing** — so the video instantly looked like someone shouting.
2. That triggered a check for the wake word, listening to the video's own soundtrack.
3. The speech system is given a hint of what to listen for — and that hint is literally **"Hey Liza."** Given a soundtrack it could not make sense of, **it handed our own hint straight back to us.** We read that as a wake word and stopped the video.

**Every single time.** Four separate fixes shipped, and the whole chain is now documented in the code so it cannot come back.

### 9.6 Six times cheaper per song

Her microphone must be switched off while music plays, or she hears the song lyrics as commands. But that would leave "stop the music" as the one thing she could never hear. So she checks periodically — and each check costs money.

| Version | How often she checks | Paid requests per song | Problem |
|---|---|---|---|
| First | every 2 sec | **about 25** | Music is the loudest thing in the room, so every check recorded the song. **Enough to hit our account limits — which then broke the speech recognition the user was waiting on.** |
| Second | every 6 sec | about 8 | "Hey Liza, stop" took too long |
| Third | every 3 sec | about 16 | The music audibly dipped every 3 seconds, all song long |
| **Now** | every 10 sec | **about 4** | **A real voice no longer waits for this check at all** — speaking up is detected instantly and costs nothing |

**Six times cheaper, no more dipping, and faster to respond than any earlier version.**

---

## 10. Doing things, not just answering

Liza does not only talk. She opens and closes your files, lists what is in a folder, changes what is on screen, plays media, and can run commands on the device.

> **How we split the work:** the **AI decides what you asked for. Our own code decides what is allowed.** People phrase requests in endless ways across two languages, which is what AI is good at. But **what is permitted is never left to the AI** — an instruction is a request, and this needs to be a guarantee.

### 10.1 Safety

| Protection | How it works |
|---|---|
| **Administrator commands blocked** | Refused by the device outright, and the AI is told it cannot override this |
| **Every part of a command checked** | *"date; sudo reboot"* is two commands, and **only the second one matters.** Checking just the first word would let it straight through |
| **Commands that destroy the device blocked** | Every way of writing "delete everything" — and **one spelling with a trailing slash slipped past an earlier version**, which is exactly why this is tested rather than assumed |
| **Cannot leave your home folder** | Any attempt to reach system locations is refused |
| **Time-limited** | Everything stops after 15 seconds |

**The line is drawn at administrator access, deliberately.** It is the owner's device. Anything they could type themselves, they can now say out loud. What is blocked is the small set of commands where **being misheard once is unrecoverable.**

### 10.2 The bug that made her permanently deaf

> **The problem** — After successfully closing a video, she stopped hearing anything for the rest of the session.
>
> **What we found** — Closing a file cleared one internal note but not another. The device was left believing something was still playing forever, so the microphone stayed in "music is playing" mode — **which means barely listening.**
>
> **What we did** — That note is now always cleared first, no matter how the file was closed.

---

## 11. The screen

| Version | What it was | Why it changed |
|---|---|---|
| First | Flat picture-by-picture animations, about 38 MB each | Too heavy and slow |
| Second | A simple shape drawn by the program that reacted to what she was doing | Removed the heavy image files |
| Third | Proper cards for the clock, weather and music, with soft shadows | The screen toolkit cannot draw shadows or gradients, so we draw them ourselves as images |
| **Now** | A 3D animated character with four moods | See below |

**Fitting a 3D character into a tiny computer:** the original animations are full HD with transparency, about 200 frames each. Loading all four would need **several gigabytes of memory** — far more than this device has. So the first time it runs, it trims each animation down to just the character, shrinks it, and saves that small version to disk. Only the small version is ever loaded. The full-screen version is built **only if someone actually asks for that mode** — holding all four at that size would take 220 MB for a feature that might never be used.

**Details we measured rather than guessed:** she stands *on* the ground in the background image rather than floating above it, because we found the exact height of the horizon in the artwork instead of estimating it.

**Three responsiveness fixes:** pressing "stop listening" used to also wake her up in the same tap. Pressing a mode button appeared to do nothing, because it waited for the current answer to finish. And we removed an entire on-screen subtitle system that was costing us a background task and a bigger download on every single reply — **to display something we had already removed from the screen.**

---

## 12. What still is not perfect

We would rather you hear this from us.

| Limitation | Where it stands | What would fix it |
|---|---|---|
| **Interrupting her works in the gaps between sentences, not mid-sentence** | Measured, understood, documented in Section 6 | A different audio setup that keeps both sides in sync — the software already exists on the device, it needs wiring in |
| **Turning the speaker above about 80% makes interrupting harder** | Known | The same fix, or a headset-style microphone |
| **Speech, thinking and voice all need internet** | A deliberate choice, for quality | On-device AI is improving fast. We already shipped a fully local voice once and replaced it because the quality was not good enough |
| **Speaking very quietly over loud music takes up to 10 seconds to register** | A deliberate trade-off | Making it more sensitive causes the music to dip constantly. We measured it, and it is not worth it |
| **Some song titles are mangled beyond recovery by pattern matching** | Handled by the AI as a fallback | Nothing further is worth the cost |

---

## 13. What this record shows

**1. We measure instead of assuming.**
The biggest fault in the project — a fifth of all sound being thrown away — produced no error message, and no user ever described it accurately. It was found by measuring. The same is true of the microphone setting, the drifting threshold, and the discount we were never receiving.

**2. We report our failures honestly.**
The echo canceller was built properly, tested properly, and rejected on the evidence. The impressive-looking 10 dB improvement was traced to a side effect and shown to make things slightly worse. **That discipline is what stops a good demo becoming an expensive support problem.**

**3. Every setting in the product can be defended.**
Not one number in the configuration is a guess. Each one sits in the code next to the measurement that produced it, and next to the values we tried that did not work. **A new engineer can change any of them and know exactly what they are trading away.**

**4. Real users found real problems, and the causes were always deeper than the symptoms.**
"It closes the video after a second" turned out to be a chain of three separate faults ending in a speech system repeating our own words back to us. "I have to say it twice" was two unrelated bugs. **No amount of automated testing would have found either.**

**5. The failures we now guard against are the expensive ones** — going completely deaf, going completely silent, giving a confident wrong answer, or claiming to have done something it never did. Each one happened. Each one was found, understood and closed.

---

*Every figure in this document was measured on the actual device, in the actual room, with the actual microphone.*
