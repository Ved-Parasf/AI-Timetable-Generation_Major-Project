"""Safe, source-grounded typo correction for conversational timetable questions.

Runs *before* routing, so corrected language is understood by every registered
read-only tool (including multi-turn followups).  Never corrects to a teacher,
course or section not present in the selected workbook/current timetable.
Ambiguous identities are reported to the user instead of guessed.

The correction layer is intentionally offline; it does not require Ollama.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
import re

_WORD = re.compile(r"[A-Za-z]+")
_TOKEN = re.compile(r"[A-Za-z0-9]+")

# Common *intent words*, not individual fixed question templates.  Qwen can
# interpret unfamiliar wordings after these high-confidence typos are fixed.
_INTENT_WORDS = '''
who whom whose what which when where why how is are was were can could would
should do does did have has had any all only other another else besides apart
from than same this that these those she he her him his their they them teacher
teachers faculty professor professors instructor instructors lecturer lecturers
maam madam sir teaching teaches teach taught teaching taught handle handles
handled responsible assigned assignment allocation allocations section sections
batch batches group groups subject subjects course courses module modules
class classes lecture lectures theory practical practicals lab labs laboratory
laboratories classroom classrooms room rooms timetable schedule scheduled
scheduling schedules periods period slot slots day days weekday weekdays daily
weekly week weeks whole entire complete monday tuesday wednesday thursday friday saturday sunday free available
availability unavailable busy occupied empty vacant unallocated open spare find
list show tell give check verify compare comparison better worse difference
count number many total highest lowest most least busiest workload load hour
hours time times morning afternoon evening first last next previous between
more fewer less greater exceeding exceed exceeds above below over under
minimum maximum per every each than at least at most
consecutive continuous back-to-back two three four meeting meet possible
please request change move moving shift shifting reschedule rescheduling swap
alternative alternatives suggest suggestion suitable suitable conflict conflicts
clash clashes overlap capacity restrictions restricted allowed policy policies
constraint constraints fairness standard optimized standard saved generated version
versions current selected valid validation approve approval approved rejected
mentoring mentor no yes not without with at on in for to of a an the and or
than teaching period periods start starting ending end friday tuesday wednesday
'''.split()
_INTENT = frozenset(w for w in _INTENT_WORDS if w)
_DAY_NAMES = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
# Safe frequent short misspellings cannot be recognized by edit distance alone.
_SHORT_TYPOS = {'whn':'when', 'wen':'when', 'wht':'what', 'whch':'which',
                'fre':'free', 'frre':'free', 'teh':'the', 'thq':'the', 'teachs':'teaches',
                'teachrs':'teachers', 'teachr':'teacher', 'tchr':'teacher',
                'avlble':'available', 'availble':'available', 'subjecst':'subjects', 'peroid':'period',
                'peroids':'periods', 'plz':'please'}


@dataclass(frozen=True)
class SpellingResult:
    question: str
    corrections: tuple[tuple[str, str], ...] = ()
    clarification: str | None = None


def _distance(a: str, b: str) -> int:
    """Adjacent transpositions count as a single typo (Damerau OSA)."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 3:
        return 99
    previous_previous = None
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cost = (x != y)
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + cost)
            if (previous_previous is not None and i > 1 and j > 1
                    and a[i - 2] == y and x == b[j - 2]):
                current[j] = min(current[j], previous_previous[j - 2] + 1)
        previous_previous, previous = previous, current
    return previous[-1]


def _limit(token: str) -> int:
    return 2 if len(token) >= 8 else 1 if len(token) >= 4 else 0


def _replace_span(text, start, end, substitute):
    return text[:start] + substitute + text[end:]


def _candidate_phrases(text, count):
    tokens = list(_TOKEN.finditer(text))
    for i in range(len(tokens) - count + 1):
        window = tokens[i:i+count]
        yield window[0].start(), window[-1].end(), [t.group().lower() for t in window]


def _distinct_teacher_names(data, rows):
    names = {str(r.get('faculty_name') or '').strip() for r in data.get('Faculty', [])}
    names |= {str(r.get('faculty_name') or '').strip() for r in rows}
    return sorted(n for n in names if n)


def _catalog(data, rows):
    catalog = {}
    for r in data.get('Courses', []):
        code = str(r.get('course_id') or '').strip()
        if code:
            catalog[code] = str(r.get('course_name') or '').strip()
    for r in rows:
        code = str(r.get('course_id') or '').strip()
        if code and code not in catalog:
            catalog[code] = str(r.get('course_name') or '').strip()
    return catalog


def _replace_teacher(text, names):
    """Correct full multi-part faculty names, including two in one question.

    Exact names win over fuzzy ones for the same span. Each candidate span
    must map to one unique faculty name; ties are never guessed.
    """
    spans = defaultdict(list)
    for name in names:
        components = re.findall(r'[a-z0-9]+', name.lower())
        if len(components) < 2:
            continue
        for start, end, window in _candidate_phrases(text, len(components)):
            distances = [_distance(a, b) for a, b in zip(components, window)]
            if not all(d <= _limit(a) for a, d in zip(components, distances)):
                continue
            score = sum(distances)
            if score <= 3:
                spans[(start, end)].append((score, name))
    if not spans:
        return text, (), None
    # Protect spans that match an exact faculty name; an adjacent typo should
    # never override an already-identified teacher in the same question.
    exact_spans = [(s, e) for (s, e), candidates in spans.items()
                   if any(d == 0 for d, _ in candidates)]
    replacements=[]
    for (start,end), candidates in spans.items():
        if (start,end) in exact_spans:
            continue
        if any(start < ee and end > ss for ss,ee in exact_spans):
            continue
        best=min(d for d,_ in candidates)
        options=sorted({name for d,name in candidates if d == best})
        if len(options)>1:
            if best<=2:
                return text, (), ('I found more than one possible faculty name: '
                                  + ', '.join(f'**{n}**' for n in options[:5])
                                  + '. Which teacher do you mean?')
            continue
        replacements.append((start,end,options[0],best))
    # Do not let overlapping fuzzy spans both rewrite the user's question.
    accepted=[]
    for start,end,name,score in sorted(replacements,key=lambda r:(r[3],r[0])):
        if not any(start < ee and end > ss for ss,ee,*_ in accepted):
            accepted.append((start,end,name,score))
    updates=[]
    for start,end,name,score in sorted(accepted,key=lambda x:x[0],reverse=True):
        updates.append((text[start:end],name))
        text=_replace_span(text,start,end,name)
    return text, tuple(reversed(updates)), None


def _replace_course_code(text, catalog):
    """Correct unique letter/prefix mistakes in known codes, not numeric IDs."""
    codes = {re.sub(r'[^A-Z0-9]', '', c.upper()): c for c in catalog}
    regex = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{2,6})[\s_.-]*([0-9OoIl]{2,5}(?:[\s_.-]*[A-Za-z])?)(?![A-Za-z0-9])')
    alterations=[]
    for hit in regex.finditer(text):
        prefix, raw_tail = hit.group(1).upper(), hit.group(2).upper()
        compact = prefix + re.sub(r'[^A-Z0-9]', '', raw_tail)
        if compact in codes:
            actual=codes[compact]
        else:
            tail=compact[len(prefix):].replace('O','0')
            if prefix+tail in codes:
                actual=codes[prefix+tail]
            else:
                tail_alpha=re.search(r'[A-Z]+$',tail)
                suffix=tail_alpha.group() if tail_alpha else ''
                digits=tail[:len(tail)-len(suffix)] if suffix else tail
                if not digits.isdigit():
                    continue
                # If the prefix itself is a real course family, a missing
                # numeric code must not silently become another department's
                # course. Example: unknown CLL240 must not become CSL240.
                known_prefixes={re.match(r'[A-Z]+',code).group()
                                for code in codes if re.match(r'[A-Z]+',code)}
                if prefix in known_prefixes:
                    continue
                possibilities=[]
                for canonical, candidate in codes.items():
                    m=re.fullmatch(r'([A-Z]{2,6})([0-9]{2,5}[A-Z]?)',canonical)
                    if m and m.group(2)==digits+suffix and _distance(prefix,m.group(1))<=1:
                        possibilities.append(candidate)
                if len(possibilities)==1:
                    actual=possibilities[0]
                elif len(possibilities)>1:
                    return text, (), ('This course code could refer to '
                                      + ', '.join(f'**{c}**' for c in sorted(possibilities)[:5])
                                      + '. Which course do you mean?')
                else:
                    continue
        if hit.group()!=actual:
            alterations.append((hit.start(),hit.end(),actual,hit.group()))
    for start,end,actual,_ in reversed(alterations):
        text=_replace_span(text,start,end,actual)
    return text, tuple((old,new) for _,_,new,old in alterations), None


def _replace_course_title(text, catalog):
    """Correct whole known course titles, not vague substrings like 'Data'."""
    titles = defaultdict(set)
    for code, title in catalog.items():
        clean = re.findall(r'[a-z0-9]+', title.lower())
        if clean and sum(len(w) for w in clean) >= 7:
            titles[tuple(clean)].add(title)
    if not titles:
        return text, (), None
    matches = []
    for components, originals in titles.items():
        for start, end, window in _candidate_phrases(text, len(components)):
            dists = [_distance(a, b) for a,b in zip(components,window)]
            if not all(d <= _limit(a) for a,d in zip(components,dists)):
                continue
            score=sum(dists)
            # A whole title requires enough information to avoid matching
            # ordinary sentence fragments. Exact titles always win.
            if score<=max(1,min(3,len(components)+1)):
                matches.append((score, start, end, sorted(originals)[0]))
    if not matches:
        return text, (), None
    best_score=min(m[0] for m in matches)
    best=[m for m in matches if m[0]==best_score]
    distinct=sorted({m[3] for m in best})
    if len(distinct)>1:
        if best_score <= 2:
            return text, (), ('This subject name could mean '
                              + ', '.join(f'**{t}**' for t in distinct[:5])
                              + '. Which one do you mean?')
        return text, (), None
    if best_score==0:
        return text, (), None
    _,start,end,chosen=min(best,key=lambda x:x[1])
    return _replace_span(text,start,end,chosen), ((text[start:end],chosen),), None


def _replace_intent_words(text, data, rows):
    """Correct likely typos in routing words, without rewriting person names."""
    preserved=set()
    for name in _distinct_teacher_names(data,rows):
        preserved.update(re.findall(r'[a-z0-9]+',name.lower()))
    for title in _catalog(data, rows).values():
        preserved.update(re.findall(r'[a-z0-9]+',title.lower()))
    # Avoid generic correction of user entities, room identifiers and numbers.
    # Keep unrecognized words untouched rather than making a low-confidence
    # change simply to satisfy a keyword-based rule.
    matches=list(_WORD.finditer(text))
    alterations=[]
    for token in matches:
        source=token.group()
        lower=source.lower()
        if lower in _SHORT_TYPOS:
            alterations.append((token.start(),token.end(),_SHORT_TYPOS[lower],source))
            continue
        if len(lower)<4 or lower in _INTENT or lower in preserved:
            continue
        # Avoid capitals inside IDs and acronyms (BTECHCSE, RN, DMAC, AI).
        if source.isupper() or (source[1:] != source[1:].lower() and source[0].isupper()):
            continue
        direct=_SHORT_TYPOS.get(lower)
        if direct:
            alterations.append((token.start(),token.end(),direct,source))
            continue
        possible=[]
        for keyword in _INTENT:
            if abs(len(keyword)-len(lower))>_limit(lower):
                continue
            d=_distance(lower,keyword)
            if d and d<=_limit(lower):
                possible.append((d,keyword))
        if not possible:
            continue
        score=min(p[0] for p in possible)
        winners={w for d,w in possible if d==score}
        if len(winners)!=1:
            continue
        word=next(iter(winners))
        # Stronger score for longer words; short words are too easy to
        # mistakenly turn into an entirely different meaning.
        if len(lower)<=5 and lower[0]!=word[0]:
            continue
        alterations.append((token.start(),token.end(),word,source))
    for start,end,replace,old in reversed(alterations):
        text=_replace_span(text,start,end,replace)
    return text, tuple((old,new) for _,_,new,old in alterations)


def _replace_group_ids(text, data, rows):
    """Correct the *prefix* of an explicit section ID, never its unique suffix.

    If 'BTECHCSEE-V-AA' maps uniquely to BTECHCSE-V-AA it is safe to
    correct; 'BTECHCSE-V-AB' is a different section and must stay different.
    Batch markers/numbers must also match exactly.
    """
    groups={str(r.get('group_id') or '').strip() for r in data.get('Student_Groups',[])}
    groups.update(str(r.get('group_id') or '').strip() for r in rows)
    pattern=re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{2,20})[-_ ]+(III|VII|VIII|IV|VI|IX|II|V|I|X)[-_ ]+([A-Za-z0-9]{1,4})(?:__([A-Za-z]{2,4})[_-]([0-9]{1,2}))?(?![A-Za-z0-9])',re.I)
    index=defaultdict(list)
    for group in groups:
        hit=pattern.fullmatch(group)
        if hit:
            index[(hit.group(2).upper(),hit.group(3).upper(),
                   (hit.group(4) or '').upper(),hit.group(5) or '')].append((hit.group(1).upper(),group))
    edits=[]
    for hit in pattern.finditer(text):
        suffix=(hit.group(2).upper(),hit.group(3).upper(),
                (hit.group(4) or '').upper(),hit.group(5) or '')
        options=index.get(suffix,[])
        raw=hit.group(1).upper()
        if not options or any(prefix==raw for prefix,_ in options):
            continue
        candidates=[(prefix,group) for prefix,group in options
                    if _distance(raw,prefix)<=_limit(prefix.lower())]
        distinct=sorted({g for _,g in candidates})
        if len(distinct)==1:
            edits.append((hit.start(),hit.end(),distinct[0],hit.group()))
        elif len(distinct)>1:
            return text,(),('This section ID could refer to '
                           + ', '.join(f'**{v}**' for v in distinct[:5])
                           + '. Which section do you mean?')
    for start,end,correct,_ in reversed(edits):
        text=_replace_span(text,start,end,correct)
    return text,tuple((old,new) for _,_,new,old in edits),None


def _replace_room_ids(text, data, rows):
    """Accept O/0 typing confusion in a known room ID; do not guess numbers."""
    rooms={str(r.get('room_id') or '').strip() for r in data.get('Rooms',[])}
    rooms.update(str(r.get('room_id') or '').strip() for r in rows)
    known={re.sub(r'[^A-Z0-9]','',x.upper()):x for x in rooms if x}
    # Zero-padding is a formatting difference, not a different room. Map
    # RN36 to RN 036 ONLY when the workbook identifies exactly one match.
    number_index={}
    for canonical in rooms:
        match=re.fullmatch(r'RN[\s_-]*([0-9]{1,4})', canonical, re.I)
        if match:
            number_index.setdefault(int(match.group(1)), set()).add(canonical)
    edits=[]
    for m in re.finditer(r'(?<!\w)RN[\s_-]*([0-9Oo]{1,4})(?!\w)',text,re.I):
        digits=m.group(1).upper().replace('O','0')
        code='RN'+digits
        options=number_index.get(int(digits), set())
        canonical=known.get(code) or (next(iter(options)) if len(options)==1 else None)
        if canonical and m.group()!=canonical:
            edits.append((m.start(),m.end(),canonical,m.group()))
    for start,end,correct,_ in reversed(edits):
        text=_replace_span(text,start,end,correct)
    return text,tuple((old,new) for _,_,new,old in edits)

def _replace_weekdays(text):
    changes=[]
    words=list(_WORD.finditer(text))
    for hit in words:
        word=hit.group()
        if word.lower() in _DAY_NAMES or len(word)<5:
            continue
        options=[]
        for day in _DAY_NAMES:
            d=_distance(word.lower(),day)
            if d<=2 and len(word)>=len(day)-2:
                options.append((d,day))
        if not options:continue
        best=min(d for d,_ in options)
        candidates={day for d,day in options if d==best}
        if len(candidates)==1:
            day=next(iter(candidates))
            changes.append((hit.start(),hit.end(),day,word))
    for s,e,day,_ in reversed(changes):
        text=_replace_span(text,s,e,day)
    return text,tuple((old,new) for _,_,new,old in changes)

def _unresolved_id(text, data, rows):
    """Explicit unknown IDs require clarification, not an unrelated answer.

    This is checked after source-grounded typo correction. IDs need to exist
    in the currently selected dataset; their digits are never guessed.
    """
    known_courses={re.sub(r'[^A-Z0-9]','',x.upper())
                   for x in _catalog(data,rows)}
    known_prefixes={re.match(r'[A-Z]+',code).group()
                    for code in known_courses if re.match(r'[A-Z]+',code)}
    course_re=re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{2,6})[\s_.-]*([0-9]{2,5}[A-Za-z]?)(?![A-Za-z0-9])')
    for hit in course_re.finditer(text):
        prefix=hit.group(1).upper()
        # Do not misread clock times ('at 11:30'), capacities ('least 50'),
        # or room codes (RN 124) as subjects.
        if prefix not in known_prefixes:
            continue
        raw=prefix+hit.group(2).upper()
        if raw not in known_courses:
            return (f'I could not find course **{raw}** in the selected dataset. '
                    'Which course did you mean? You can give its subject name.')
    rooms={re.sub(r'[^A-Z0-9]','',str(r.get('room_id') or '').upper())
           for r in data.get('Rooms',[])}
    rooms.update(re.sub(r'[^A-Z0-9]','',str(r.get('room_id') or '').upper())
                 for r in rows)
    for hit in re.finditer(r'(?<!\w)RN[\s_-]*([0-9Oo]{2,4})(?!\w)',text,re.I):
        raw='RN'+hit.group(1).upper().replace('O','0')
        if raw not in rooms:
            return (f'I could not find room **{raw[:2]} {raw[2:]}** in the selected dataset. '
                    'Please check the room number.')
    return None

def normalize_question(question, data, rows=()):
    """Return a corrected question or ask for clarification of ambiguous IDs.

    No changes are ever applied to the workbook, timetable, chat history or
    schedule.  This function only prepares the user's *current query*.
    """
    text=str(question or '').strip()
    if not text:
        return SpellingResult(text)
    changes=[]
    # Separate an accidentally joined "is" + exact teacher first name, e.g.
    # "when issneha maam available" -> "when is sneha maam available".
    # Only do this when the first name is unambiguous in the workbook.
    given_names=defaultdict(set)
    for full_name in _distinct_teacher_names(data, rows):
        first=full_name.split()[0].lower()
        if len(first)>=4:
            given_names[first].add(full_name)
    joined=re.compile(r'(?<![A-Za-z])is([A-Za-z]{4,})(?![A-Za-z])', re.I)
    replacements=[]
    for hit in joined.finditer(text):
        name=hit.group(1).lower()
        if len(given_names.get(name, ()))==1:
            replacements.append((hit.start(),hit.end(),'is '+hit.group(1)))
    for start,end,correct in reversed(replacements):
        changes.append((text[start:end],correct))
        text=_replace_span(text,start,end,correct)
    for fn,args in (
        (_replace_course_code,(_catalog(data,rows),)),
        (_replace_group_ids,(data,rows)),
        (_replace_teacher,(_distinct_teacher_names(data,rows),)),
        (_replace_course_title,(_catalog(data,rows),)),
    ):
        text,found,clarification=fn(text,*args)
        changes.extend(found)
        if clarification:
            return SpellingResult(question, tuple(changes), clarification)
    # Known-word correction happens after entity matching, so it cannot turn
    # an intended course/teacher into an ordinary English word.
    text,found=_replace_room_ids(text,data,rows)
    changes.extend(found)
    invalid_identifier=_unresolved_id(text,data,rows)
    if invalid_identifier:
        return SpellingResult(text,tuple(changes),invalid_identifier)
    text,found=_replace_intent_words(text,data,rows)
    changes.extend(found)
    text,found=_replace_weekdays(text)
    changes.extend(found)
    return SpellingResult(text,tuple(changes))
