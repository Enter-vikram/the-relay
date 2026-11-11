# Benchmark questions (to be authored from the real PDFs)

`benchmarks/questions.jsonl` does **not exist yet on purpose**: the questions, gold evidence and required
facts must come from reading the three PDFs, not from memory and not from an AI that has not read them.

## How to build it (about 1 to 2 hours for 18 questions)
1. Run `setup.bat` (downloads + verifies the PDFs, extracts text, builds the index).
2. Copy `benchmarks/questions.template.jsonl` to `benchmarks/questions.jsonl`, delete the TEMPLATE row.
3. For each question, open the PDF, find the passage, copy 6+ words of it verbatim into `evidence.quote`
   and note the PDF page (1-based, the page number in the PDF viewer, not the printed number).
4. Run `python -m experiments.validate_benchmark`. It fails if any quote is not on the stated page,
   if ids repeat, if fewer than 15 questions exist, and warns about missing question types or papers.

## What to include (the assignment's own list)
| type | what it is | expect_refusal |
|---|---|---|
| direct | one fact in one passage | false |
| multi_passage | needs two or more passages (same or different paper) | false |
| comparison | asks how two of the papers (or two methods) differ | false |
| interpretation | answer needs careful reading (a number with a condition, a negation) | false |
| unsupported | sounds related but the papers do not establish it | **true** |
| misleading | shares vocabulary with the wrong passage; fill `trap` with what a naive retriever would grab | false or true |

Aim for at least 3 `unsupported`, 3 `misleading`, all three papers covered, and 18 or more rows so that every
percentage in Table 3 rests on a visible denominator. Percentages on 15 questions move 6.7 points per
question: report counts next to them.

## `required_facts` (how answers are graded without trusting the model)
A list of groups; the answer must contain one alternative from **every** group (case-insensitive substring).
Example shape (placeholder, not a real fact): `[["8 heads", "eight heads"], ["64"]]`.
Citations must additionally cover `min_cited` distinct evidence items (same paper, page inside the cited chunk's
page range). Borderline results are written to `results/grading_review.csv` for you to read by hand.
