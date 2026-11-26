"""Build a local, initially unlabelled semantic review form for saved results."""

import argparse
import json
from pathlib import Path

from .data import load_fragments
from .evaluate import rows, score
from .runner import ROOT, digest
from .workflow import strict_json


def build(session, gold, output, root=ROOT):
    session, gold, output, root = map(Path, (session, gold, output, root))
    score(session, gold_path=gold, root=root)
    references = {r['case_id']: r for r in rows(gold)}
    fragments = {f.fragment_id: f for f in load_fragments(root / 'data/fragments.jsonl')}
    cases = []
    for path in sorted(session.glob('*/result.json')):
        case = strict_json((path.parent / 'input.json').read_text(encoding='utf-8'))
        if case['phase'] != 'J2':
            continue
        result = strict_json(path.read_text(encoding='utf-8'))
        cases.append({'case': case, 'result': result, 'gold': references[case['case_id']],
                      'fragments': {e['fragment_id']: {'text': fragments[e['fragment_id']].text,
                          'source_id': fragments[e['fragment_id']].source_id}
                                    for c in result['claims'] for e in c['evidence']}})
    payload = json.dumps({'cases': cases, 'gold_sha256': digest(gold)}, ensure_ascii=False).replace('<', '\\u003c')
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка ответов</title><style>body{font:16px/1.5 system-ui;max-width:1000px;margin:32px auto;padding:0 20px;background:#f5f7fa;color:#1d2630}section{padding:22px;background:white;border:1px solid #d2d9e1;border-radius:10px;margin:22px 0}select,input,textarea,button{font:inherit;padding:7px;margin:5px}textarea{width:95%}pre{white-space:pre-wrap;font:14px/1.5 system-ui}label{display:block;margin:12px 0}.error{color:#a82020}summary{cursor:pointer}</style>
<h1>Проверка ответов</h1><p>Сопоставьте утверждения и основания с вопросом и эталоном. Дословность цитаты не гарантирует полноту ответа. Исходные оценки не заполнены. Черновик хранится в этом браузере.</p>
<label>Ваш идентификатор <input id="reviewer" autocomplete="off"></label><p id="progress"></p><div id="cases"></div><button id="export">Скачать оценки JSONL</button><p id="error" class="error"></p>
<script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent), key='labestra-review-'+data.gold_sha256+'-'+data.cases.map(x=>x.result.run_id).join(',');
let saved={};try{saved=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
const state=saved.state||{}, reviewer=document.getElementById('reviewer');reviewer.value=saved.reviewer||'';
const field=(tag,parent,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;parent.append(e);return e;};
function store(){localStorage.setItem(key,JSON.stringify({reviewer:reviewer.value,state}));document.getElementById('progress').textContent='Подтверждено '+Object.values(state).filter(x=>x.confirmed).length+' / '+data.cases.length;}
reviewer.oninput=store;
for(const item of data.cases){
 const id=item.result.run_id, s=state[id]||(state[id]={correct:{},entailed:{},covered:{},accepted:'',rationale:'',confirmed:false});
 const section=field('section',document.getElementById('cases'));field('h2',section,item.case.case_id);field('p',section,item.case.question);field('h3',section,'Ответ');field('p',section,item.result.answer);
 const controls=[];
 function choose(title,obj,k){const label=field('label',section,title), select=field('select',label);for(const [v,t] of [['','Выберите'],['true','Да'],['false','Нет']]){const op=field('option',select,t);op.value=v;}select.value=obj[k]===undefined?'':obj[k];select.onchange=()=>{obj[k]=select.value;s.confirmed=false;confirm.checked=false;store();};controls.push(select);return select;}
 field('h3',section,'Корректность и основания');
 for(const claim of item.result.claims){
  field('p',section,claim.claim_id+': '+claim.text);choose('Утверждение корректно с учётом контекста?',s.correct,claim.claim_id);
  for(const evidence of claim.evidence){const f=item.fragments[evidence.fragment_id],details=field('details',section);field('summary',details,f.source_id+' · '+evidence.fragment_id.slice(0,12));field('pre',details,f.text);choose('Фрагмент поддерживает указанную связь?',s.entailed,claim.claim_id+'/'+evidence.fragment_id);}
 }
 const truthControls=controls.slice();
 const bulk=field('button',section,'Отметить все утверждения и связи верными');bulk.onclick=()=>{for(const c of item.result.claims){s.correct[c.claim_id]='true';for(const e of c.evidence)s.entailed[c.claim_id+'/'+e.fragment_id]='true';}for(const select of truthControls)select.value='true';s.confirmed=false;confirm.checked=false;store();};
 field('h3',section,'Полнота');field('p',section,'Ожидаемое действие: '+item.gold.expected_action);
 for(const c of item.gold.required_claims){field('p',section,c.text+(c.conditions?' · '+JSON.stringify(c.conditions):''));choose('Этот обязательный вывод отражён корректно?',s.covered,c.gold_claim_id);}
 const accepted=choose('Ответ полностью решает задачу и может быть принят?',s,'accepted');
 const label=field('label',section,'Причина оценки'), rationale=field('textarea',label);rationale.value=s.rationale;rationale.oninput=()=>{s.rationale=rationale.value;s.confirmed=false;confirm.checked=false;store();};
 const confirmLabel=field('label',section),confirm=field('input',confirmLabel);confirm.type='checkbox';confirm.checked=s.confirmed;field('span',confirmLabel,' Я проверил этот ответ и основания');confirm.onchange=()=>{s.confirmed=confirm.checked;store();};
}
store();
function exportRows(){
 const name=reviewer.value.trim();if(!name)throw Error('Введите идентификатор проверяющего.');
 return data.cases.map(item=>{const s=state[item.result.run_id],claims=item.result.claims;
  if(!s.confirmed||!s.rationale.trim()||!['true','false'].includes(s.accepted))throw Error('Завершите проверку '+item.case.case_id);
  for(const c of claims){if(!['true','false'].includes(s.correct[c.claim_id]))throw Error('Оцените все утверждения '+item.case.case_id);for(const e of c.evidence)if(!['true','false'].includes(s.entailed[c.claim_id+'/'+e.fragment_id]))throw Error('Оцените все связи '+item.case.case_id);}
  for(const c of item.gold.required_claims)if(!['true','false'].includes(s.covered[c.gold_claim_id]))throw Error('Оцените полноту '+item.case.case_id);
  if(s.accepted==='true'&&(item.gold.expected_action!=='answer'||!claims.length||Object.values(s.correct).some(v=>v!=='true')||Object.values(s.entailed).some(v=>v!=='true')||item.gold.required_claims.some(c=>s.covered[c.gold_claim_id]!=='true')))throw Error('Принятие требует корректности и полного покрытия: '+item.case.case_id);
  return {run_id:item.result.run_id,reviewer_id:name,rationale:s.rationale,claim_correct:Object.fromEntries(Object.entries(s.correct).map(([k,v])=>[k,v==='true'])),evidence_entailed:Object.fromEntries(Object.entries(s.entailed).map(([k,v])=>[k,v==='true'])),covered_gold_claim_ids:item.gold.required_claims.filter(c=>s.covered[c.gold_claim_id]==='true').map(c=>c.gold_claim_id),accepted:s.accepted==='true'};
 });
}
document.getElementById('export').onclick=()=>{try{const text=exportRows().map(r=>JSON.stringify(r)).join('\\n')+'\\n',url=URL.createObjectURL(new Blob([text],{type:'application/x-ndjson'})),a=document.createElement('a');a.href=url;a.download='baseline-ratings.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);document.getElementById('error').textContent='';}catch(e){document.getElementById('error').textContent=e.message;}};
</script></html>'''.replace('PAYLOAD', payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(page)
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        count = build(args.session, args.gold, args.output)
        print(f'Создана форма на {count} ответов: {args.output}')
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'Подготовка формы остановлена: {exc}\n')


if __name__=='__main__':
    main()
