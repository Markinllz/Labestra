"""Deterministic BM25 search and extractive answers; no model or reference labels."""

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import re

from .schemas import Claim, Corpus, Evidence, RunResult, ToolFailure
from .workflow_runtime import StopRun

POLICY = 'extractive-v1'
SOURCE_LIMIT = 3
QUOTE_LIMIT = 3
MAX_QUOTE_CHARS = 600


def terms(text):
    return re.findall(r'[^\W_]+', text.casefold(), flags=re.UNICODE)


def extract_quotes(query, fragments):
    """Rank verbatim spans by query overlap/IDF, retaining source diversity.

    Ties preserve fragment-search order and span offsets. All policies are fixed
    globally, with no per-case rules or access to gold/ratings.
    """
    query_terms = set(terms(query))
    candidates = []
    for rank, fragment in enumerate(fragments):
        for match in re.finditer(r'\S[\s\S]*?(?:[.!?](?=\s|$)|\Z)', fragment.text):
            start, end = match.span()
            while end > start and fragment.text[end - 1].isspace():
                end -= 1
            if end - start > MAX_QUOTE_CHARS:
                end = start + MAX_QUOTE_CHARS
                # Never concatenate or paraphrase: the output stays one source span.
                boundary = fragment.text.rfind(' ', start, end)
                if boundary > start:
                    end = boundary
            text = fragment.text[start:end]
            tokens = set(terms(text))
            if query_terms & tokens:
                candidates.append({'fragment': fragment, 'rank': rank, 'start': start,
                                   'end': end, 'text': text, 'terms': tokens})
    frequencies = Counter(t for candidate in candidates for t in candidate['terms'])
    for candidate in candidates:
        candidate['score'] = sum(math.log(1 + (len(candidates) + 1) / (frequencies[t] + 1))
                                 for t in query_terms & candidate['terms'])
    candidates.sort(key=lambda c: (-c['score'], c['rank'], c['start']))
    # Deduplicate overlapping passage text before reserving one quote per source.
    unique, texts = [], set()
    for candidate in candidates:
        normalized = ' '.join(candidate['text'].split())
        if normalized not in texts:
            texts.add(normalized)
            unique.append(candidate)
    selected, sources = [], set()
    for candidate in unique:
        sid = candidate['fragment'].source_id
        if sid not in sources:
            selected.append(candidate)
            sources.add(sid)
            if len(selected) == QUOTE_LIMIT:
                break
    if len(selected) < QUOTE_LIMIT:
        selected.extend(c for c in unique if c not in selected)
        selected = selected[:QUOTE_LIMIT]
    return selected


def run_extractive(case, tools, config, run_id, *, services):
    """Use the shared wrapped tools and return the ordinary RunResult schema."""
    rt = services.runtime
    ranked, corpus_id, claims = [], None, []
    answer, evidence = '', 'not_assessed'
    limitations = ['Дословные цитаты; переформулирование, синтез и смысловая проверка полноты отсутствуют.']
    def finish(status, reason):
        return RunResult(run_id, case.case_id, 'baseline', rt.mode, status, answer, claims,
                         limitations, evidence, reason, rt.usage(), rt.duration,
                         retrieved_source_ids=ranked, corpus_revision_id=corpus_id)
    try:
        rt.checkpoint()
        rt.emit('baseline_policy', output={'policy': POLICY, 'source_limit': SOURCE_LIMIT,
                                          'quote_limit': QUOTE_LIMIT, 'max_quote_chars': MAX_QUOTE_CHARS})
        if not case.search_query.strip():
            answer = 'Уточните задачу и задайте английскую поисковую фразу.'
            return finish('needs_clarification', 'search_query_required')
        if case.phase == 'J1':
            hits = tools.search_sources(case.search_query, {}, case.collection_id)
            ranked = [source.source_id for source in hits]
            rt.emit('initial_ranking', output={'source_ids': ranked})
            if not ranked:
                evidence = 'insufficient_evidence'
                answer = 'Источники не найдены.'
                return finish('completed', 'empty_search')
            chosen = ranked[:SOURCE_LIMIT]
            corpus_id = 'extractive-' + hashlib.sha256(json.dumps([run_id, chosen]).encode()).hexdigest()[:24]
            corpus = Corpus(corpus_id, case.collection_id, chosen)
            services.created_corpora.append(corpus)
            rt.emit('corpus_selected', output=asdict(corpus))
            answer = '\n'.join(f'[{source.source_id}] {source.title}' for source in hits[:SOURCE_LIMIT])
            return finish('completed', 'corpus_selected')
        corpus = services.corpora.get(case.corpus_revision_id)
        if corpus is None or corpus.collection_id != case.collection_id:
            raise StopRun('failed', 'invalid_corpus')
        corpus_id = corpus.corpus_revision_id
        hits = tools.search_fragments(case.search_query, corpus)
        exact = tools.read_fragments([fragment.fragment_id for fragment in hits], corpus)
        selected = extract_quotes(case.search_query, exact)
        rt.checkpoint()
        if not selected:
            evidence = 'insufficient_evidence'
            answer = 'Не найдены фрагменты с терминами запроса.'
            return finish('completed', 'insufficient_evidence')
        claims = [Claim(f'q{i}', quote['text'], [Evidence(quote['fragment'].fragment_id, 'supports')])
                  for i, quote in enumerate(selected, 1)]
        answer = '\n\n'.join(f'[{quote["fragment"].fragment_id}] {quote["text"]}' for quote in selected)
        rt.emit('extractive_answer', output={'quotes': [
            {'claim_id': f'q{i}', 'fragment_id': quote['fragment'].fragment_id,
             'start': quote['start'], 'end': quote['end'], 'score': quote['score']}
            for i, quote in enumerate(selected, 1)]})
        return finish('completed', 'draft_ready')
    except StopRun as stop:
        return finish(stop.status, stop.reason)
    except ToolFailure as exc:
        return finish('failed', exc.error.code)
