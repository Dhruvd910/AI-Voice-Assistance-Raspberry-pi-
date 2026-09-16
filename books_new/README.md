# The books

Liza searches these before she answers, so a Class 6 question is answered out
of the Class 6 chapter rather than out of the model's memory of the subject.

## What is kept

Classes **6 to 12**. Physical education, arts, Hindi-language and Sanskrit books
are skipped for now -- see `WANTED_CLASSES` and `SKIPPED_SUBJECTS` at the top of
`books.py`. A book in a skipped subject is neither downloaded nor searched, even
if it is in this folder.

## Where a book goes

    books_new/<class>/<subject>/<anything>.pdf

    books_new/VI/science/fecu101.pdf
    books_new/X/history/jess301.pdf
    books_new/XII/physics/leph101.pdf

The class folder is a Roman numeral (or `class-6`), and the folder under it is
the subject, typed however you like -- `Social_science_i`, `english_VI` and
even `pilotical_science` are all understood. Deeper folders are fine too. The
**path is the only manifest**: nothing has to be registered anywhere.

The folder name decides the subject. Where it names none, an NCERT file name
decides instead: `jess401.pdf` is Class 10 Civics wherever it is put.

## When downloads went wrong

NCERT's server cuts large downloads short. A cut-off PDF has no ending, so not
one word of it can be read, and a cut-off zip opens as nothing at all.

```bash
liza books repair       # re-fetch every NCERT PDF that was cut off
liza books zips         # unpack zips, or fetch the book a broken zip was meant to hold
```

Both work from the NCERT file names, so they only help with NCERT books. A broken
copy is replaced only once the new download has been checked to open.

## Then

```bash
liza books ingest        # read every PDF under books_new/ into the index
liza books status        # what is on the device
liza books search "how do we separate sand from water" --class 6
```

Re-running `ingest` is safe: a chapter already indexed is replaced, not doubled.

## ICSE

CISCE does not publish its textbooks, so there is nothing to download. Put your
own copies in a board folder -- `books_new/ICSE/IX/Physics/` -- and ingest them.
Until an ICSE book is on the device, an ICSE student is answered from the NCERT
passages for their class, and told plainly that it is not their own book.
