# Benchmark audit (questions.jsonl, 21 questions)

Source: `benchmarks/questions.original.jsonl` is the file the user supplied; `questions.jsonl` is currently byte-identical (no edits made). PDFs verified: 15 + 66 + 39 = 120 pages; sha256 values in `research/manifest.json`.

## Checks run
- `python -m experiments.validate_benchmark`: passes. Every evidence quote is found on its stated 1-based page of the extracted PDF text (hyphenation/ligature tolerant match). Types present: 6 direct, 3 multi_passage, 3 comparison, 3 interpretation, 3 unsupported, 3 misleading; all three papers covered.
- Extra check (not in the validator): each `required_facts` group has an alternative present on the pages of its evidence. All groups pass except **INT-03 group ["no","does not"] / ["higher"]**: "higher" is a comparison the answer must state, derived from 5.7% vs 3.6%, not text on the page. This is a legitimate reasoning requirement but makes grading depend on the word "higher" (a model saying "worse" or "greater" would be graded wrong). Not changed; review before the real run (`results/grading_review.csv` shows borderline cases).

## Known caveats (not changed)
- **ATT-M01**: the Transformer paper p.8 prose says EN-FR BLEU 41.0 while Table 2 on the same page lists 41.8. The question uses the prose (as its notes say). A model answering 41.8 is graded wrong.
- **ATT-M01** evidence quote 1 stops before the sentence containing "28.4"; the number is on the same page, in the next sentence. Fine for page-level citation grading.
- **INT-02** notes: the second evidence item is described confusingly in `notes`; the quote itself is on page 23.
- Required-fact matching is case-insensitive substring: `√dk` (ATT-D01) and `10−9` (ATT-D03, unicode minus) need alternatives such as "sqrt" or "10^-9" if a model formats them differently; only the listed alternatives count.
- Only 3 unsupported and 3 misleading questions: gate statistics on them have very small denominators.
