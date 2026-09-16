# The books

Liza searches these before she answers, so a Class 6 question is answered out
of the Class 6 chapter rather than out of the model's memory of the subject.

## Where a book goes

    books/<board>/class-<n>/<subject>/<anything>.pdf

    books/CBSE/class-6/Science/fecu101.pdf
    books/ICSE/class-9/Physics/selina-chapter-3.pdf

The **path is the only manifest**. Board, class and subject are read out of the
folder names, so a file dropped into the right shape is ready to ingest with no
list to edit. Plain `.txt` works the same way.

## CBSE

NCERT publishes every CBSE textbook chapter by chapter, and they can be
downloaded:

```bash
liza books fetch --class 6 --class 7 --medium en
```

`--medium hi` gets the Hindi editions, `--subject science` narrows it. Ask for
the classes the children on this device are actually in: the whole NCERT shelf
is several gigabytes and this is an SD card.

## ICSE

CISCE does not publish the textbooks. ICSE books are commercial — Selina,
Frank, and others — and there is no legal download, so there is nothing to
fetch. Put your own copies under `books/ICSE/class-9/Physics/` and ingest them
like anything else.

## Then

```bash
liza books ingest        # read every PDF under books/ into the index
liza books status        # what is on the device
liza books search "how do we separate sand from water" --class 6
```

Re-running `ingest` over the folder is safe: a book already in the index is
replaced rather than doubled, so the normal way to add the chapters you were
missing is to drop them in and run it again.

## A scanned book

`ingest` says "there was no readable text in it" when a PDF is page images
rather than text. Run it through OCR elsewhere first — `ocrmypdf` is the usual
answer — and ingest the result.

## Disk

A chapter PDF is 2–5MB and its text is about 40KB, so the index is far smaller
than the books. Once a book is ingested its PDF is no longer read, and deleting
it costs nothing — until you want to re-ingest.
