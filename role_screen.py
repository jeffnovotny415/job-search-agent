"""Free title screening for employment; client projects use their own rules."""
import re

BUILDER_SEARCH_TERMS = (
    'product builder', 'AI builder', 'internal tools', 'internal tools lead',
    'internal tools manager', 'automation lead', 'automation specialist',
    'AI automation', 'low-code developer', 'no-code developer',
    'technical product owner', 'product operations', 'solutions builder',
)
BUILDER = r'\b(?:builder|product owner|internal tools|(?:low|no)[- ]code|automation (?:lead|specialist)|AI automation)\b'
ENGINEER_EXCEPTION = r'\b(?:founding|software|product) engineer\b'
BLOCKED = r'\b(?:engineer|programmer|developer|scientist|auditor|cryptograph\w*|technical specialist|EPC|construction|estimator|superintendent)\b'
NOUN = r'\b(?:manager|lead|director|head|coordinator|administrator|owner|builder|architect|chief of staff)\b'
DOMAIN = r'\b(?:IT|technology|technical|systems|operations|AI|automation|implementation|platform|internal tools|workflow|product|salesforce)\b'
POSITIVE = r"\b(?:AI[- ](?:native|first)|Claude Code|Cursor|Copilot|build(?:ing)? with AI|(?:low|no)[- ]code|Retool|Airtable|Zapier|n8n|Make\.com|rapid prototyping|ship quickly|you['’]ll build the tools)\b"


def builder_candidate(title):
    return bool(re.search(BUILDER + '|' + ENGINEER_EXCEPTION, title or '', re.I))


def plausible(title):
    """An engineer exception is provisional until its description is checked."""
    title = title or ''
    if re.search(BUILDER, title, re.I):
        return True
    if re.search(BLOCKED, title, re.I):
        return bool(re.search(ENGINEER_EXCEPTION, title, re.I))
    return bool(re.search(NOUN, title, re.I) and re.search(DOMAIN, title, re.I))


def screen(job):
    """Return a hard exclusion plus non-scored flags for Claude's deeper vet."""
    job.pop('lane', None)
    job.pop('builder_signals', None)
    title = job.get('title', '')
    if not plausible(title):
        return 'Roles title requires a relevant domain and lane noun; production/specialist titles excluded', []
    if not builder_candidate(title):
        return None, []
    text = job.get('description', '') if job.get('description_verified') else ''
    signals = []
    for sentence in re.split(r'(?<=[.!?;])\s+|\n+', text):
        for match in re.finditer(POSITIVE, sentence, re.I):
            before = sentence[max(0, match.start() - 60):match.start()]
            after = sentence[match.end():match.end() + 45]
            if not re.search(r'\b(?:no|not|without|avoid|ban\w*|prohibit\w*)\b[^,;]{0,50}$', before, re.I) and not re.match(r'\s+(?:is |are )?(?:banned|prohibited|not allowed|forbidden)', after, re.I):
                signals.append(match[0])
    def required(pattern):
        for match in re.finditer(pattern, text, re.I):
            before = text[max(0, match.start() - 60):match.start()]
            after = text[match.end():match.end() + 45]
            if re.search(r"\b(?:no|not|never|without|don't)\b[^.!?;]{0,40}$", before, re.I):
                continue
            if re.match(r'\s+(?:(?:is|are) )?(?:not required|optional|not necessary|restrictions)', after, re.I):
                continue
            return True
        return False
    if required(r'\b(?:without AI tools|no AI[- ]generated code|over[- ]reliance on AI|(?:ban\w*|prohibit\w*)\s+(?:the use of )?AI|AI (?:tools|coding)\s+(?:is |are )?(?:banned|prohibited|not allowed))\b'):
        return 'Builder role explicitly restricts AI-assisted coding', []
    flags = []
    tests = (
        (r'\b(?:LeetCode|coding interview|take[- ]home algorithm test)\b', 'Builder: verify coding/algorithm interview requirements'),
        (r'\b\d+\+?\s+years?\b[^.!?\n]{0,90}\b(?:production\b[^.!?\n]{0,90}\b(?:React|Go|Rust|Java|Kubernetes)|(?:React|Go|Rust|Java|Kubernetes)\b[^.!?\n]{0,90}\bproduction)\b', 'Builder: verify required production engineering experience'),
        (r'\b(?:CS|computer science) degree\s+(?:is )?required\b|\brequir\w*\s+(?:a )?(?:CS|computer science) degree\b', 'Builder: verify required computer science degree'),
        (r'\b(?:no|not|avoid|reject\w*|unacceptable)[^.!?\n]{0,50}\bvibe[- ]cod\w*\b|\bvibe[- ]cod\w*\b[^.!?\n]{0,50}\b(?:unacceptable|not allowed|rejected)\b', 'Builder: employer discourages vibe-coded work'),
    )
    for pattern, flag in tests:
        if required(pattern):
            flags.append(flag)
    if not re.search(BUILDER, title, re.I):
        if not signals:
            return 'Production engineer title has no verified AI-assisted builder signal', []
        if flags:
            return 'Production engineer title also requires traditional engineering vetting', flags
    job['lane'] = 'Builder'
    if signals:
        job['builder_signals'] = list(dict.fromkeys(signals))[:5]
    else:
        flags.append('Builder: verify that AI-assisted building is welcome')
    return None, flags
