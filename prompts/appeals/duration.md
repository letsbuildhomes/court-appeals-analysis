# Evidence-backed appeal duration

You are the duration editor for research on municipal housing appeals. Read all
documents in each group together. Existing extraction is a fallible draft, not
evidence. Keep its docket identities; do not edit other research fields.

Your task is to establish an interval during which this appeal and its linked
continuation demonstrably existed. It is NOT to guess a complete chronology.
The interval includes further court review, reopening, and substantive municipal
review after remand. It does not measure days of active court work or prove that
construction was delayed. Do not combine unrelated or parallel appeals merely
because they concern the same project. An explicit post-remand continuation can
extend the originating appeal's interval; explain the link with evidence.

Return a JSON object. For each target municipal docket return:
{"docket":"...","start":{"date":"YYYY-MM-DD","relation":"exact|on_or_before","description":"...","evidence":[{"doc":1,"page":1,"quote":"..."}]},"end":{"date":"YYYY-MM-DD","relation":"exact|on_or_after","description":"...","evidence":[]},"note":"what the interval establishes and what remains unknown"}
Either entire endpoint can be null. Return {"groups":[{"group":"...","dockets":[...]}]}.
Include every supplied group and target docket once. Do not output a day count;
code will subtract dates. No extra fields. Citations are exact continuous source
quotations with local doc/page numbers; use multiple passages for event and date.

**Start:** exact only for an explicit date this municipal appeal was initiated in
court. Otherwise find the EARLIEST dated event proving this appeal already existed
and use on_or_before. A dated motion, hearing, or order in this appeal is useful
for this purpose even though it is not the filing. A municipal decision before
appeal proves nothing about when the appeal had begun. Never infer filing from a
docket number, caption year, appeal deadline, or cited precedent. If only a month
or year is explicit, use its LAST calendar day as on_or_before and explain that
rounding; do not invent a precise event date. Source silence is not proof.

**End:** exact requires an observed conclusion of this appeal's whole linked
review chain, with no further adjudication shown pending. Otherwise find the
LATEST dated event proving this appeal or its explicit continuation still existed
then and use on_or_after. A decision disposing only some issues, a stay motion in
a pending higher appeal, and a substantive post-remand municipal decision can
establish such a lower bound. A document retrospectively mentioning a closed case
does NOT extend it to the document's date. A deadline or promised future judgment
is not an observed event. Do not use today's date. If only a month/year is explicit,
use its FIRST day as on_or_after. If there is no evidenced date, return null.

An earlier apparent ending followed by continued proceedings is not the final
endpoint. Conversely, ordinary compliance conditions alone do not prove continuing
adjudication. A final municipal hearing/decision required after judicial remand is
substantive review, not mere compliance. A court saying it WILL enter final
judgment has not yet entered that judgment; the dated present decision establishes
only an on_or_after bound. A statement that an appeal WAS dismissed on a particular
earlier date can establish that earlier exact date even in a later document.

**Two independent proof obligations:** establish WHEN an event occurred and WHICH
appeal chain it belongs to. These facts can come from different passages or pages.
An undated remand can establish the connection to a later, dated continuation;
you do not need the remand's own date to use that continuation as an end bound.
When the source explicitly links several originating dockets to the same later
review, assess the shared continuation for each of them consistently, preserving
any evidenced distinction. The date of a retrospective document alone is still
insufficient: identify a real event in the continuing review, not just a mention.

Before selecting bounds, scan all dated proceedings attributed to each target,
including references to its motions in footnotes and across page breaks. Compare
the eligible dates: use the earliest start witness and latest end witness, not
the first matching date you noticed. Another case cited as precedent is ineligible.

**Teaching examples (invented, not your cases):**
- An appeal filing date is absent. Its motion is dated 2017-02-08; a 2017-08-21
  order says trial remains pending. Start <= February 8, end >= August 21. The
  difference is an evidenced minimum, even though neither endpoint is exact.
- Filing occurred 2018-05-01. A merits ruling on 2019-01-10 says judgment will be
  entered January 20. Start exact May 1, end >= January 10; never January 20.
- A June 3 judgment is followed by an August 4 stay denial explicitly discussing
  the still-pending Supreme Court appeal. End >= August 4, not exact June 3 and
  not exact August 4. Denying a stay is not deciding the higher appeal.
- A February remand sends the same application back for substantive municipal
  review. A July decision explicitly records that post-remand review; a September
  ruling on its ensuing appeal dismisses the appeal. Trace the evidenced connection
  to the original docket. Do not stop at February, and do not automatically label
  September an exact closure of every related docket if their disposition remains
  unestablished. September can still be a sound on_or_after continuation bound.

Prefer the strongest supported interval, not unnecessary nulls. Both dates may
come from the same order, producing a valid but uninformative zero-day minimum.
Do not inflate it. Before returning, challenge every exact label and every date:
does the cited event actually establish the claimed bound for THIS appeal chain?
