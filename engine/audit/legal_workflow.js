export const meta = {
  name: 'tmt-source-legal-analysis',
  description: 'Rigorous per-source legal reasoning: scraping-access AND copyright-reproduction, each independently, for every current and candidate source',
  phases: [
    { title: 'Analyse', detail: 'one agent per source-group, fresh verification + statutory reasoning' },
    { title: 'Consolidate', detail: 'consistency pass, final gate, PDF-ready structure' },
  ],
}
const ROOT = args.root, TODAY = args.today
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

const SRC_SCHEMA = {
  type: 'object', required: ['sources'],
  properties: {
    sources: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'authority', 'what_we_fetch', 'robots_verbatim', 'robots_verdict', 'terms_scraping_clause',
                   'access_analysis', 'access_verdict', 'reproduction_what', 'copyright_owner', 'copyright_exception',
                   'reproduction_analysis', 'reproduction_verdict', 'overall', 'residual_risk'],
        properties: {
          id: { type: 'string' },
          authority: { type: 'string' },
          url: { type: 'string' },
          what_we_fetch: { type: 'string' },
          robots_verbatim: { type: 'string' },
          robots_verdict: { type: 'string', enum: ['allows_our_path', 'disallows_our_path', 'no_robots', 'ambiguous'] },
          terms_scraping_clause: { type: 'string' },
          access_analysis: { type: 'string' },
          access_verdict: { type: 'string', enum: ['permissible', 'permissible_qualified', 'not_permissible'] },
          reproduction_what: { type: 'string' },
          copyright_owner: { type: 'string' },
          copyright_exception: { type: 'string', enum: ['s52_1_q_i_gazette', 's52_1_q_ii_act', 's52_1_q_iii_report', 's52_1_q_iv_judgment', 's52_1_a_fair_dealing', 'ebc_modak_no_copyright_in_facts', 'express_licence', 'godl', 'none'] },
          reproduction_analysis: { type: 'string' },
          reproduction_verdict: { type: 'string', enum: ['permissible', 'permissible_qualified', 'not_permissible'] },
          overall: { type: 'string', enum: ['include', 'include_metadata_only', 'include_with_conditions', 'exclude'] },
          conditions: { type: 'string' },
          residual_risk: { type: 'string' },
          recent_instrument: { type: 'string' },
        },
      },
    },
  },
}

phase('Analyse')

const GROUPS = [
  { key: 'gazette', label: 'e-Gazette (the cornerstone)', ids: 'gazette_communications, gazette_meity, gazette_mib (Gazette of India ministry search)',
    focus: 'This is the legal cornerstone. Reason hard on: no robots.txt; the About Us "may be accessed free of cost by the general public / public domain" statement vs the "with its copyright" claim (address the contradiction); IT Act s.8 (Electronic Gazette is the official channel); Copyright Act s.52(1)(q)(i) permitting reproduction of gazette matter EXCEPT an Act (and the q(ii) Act carve-out); and Bharatiya Sakshya Adhiniyam 2023 s.81 presumption of genuineness for the electronic gazette record. Verify egazette.gov.in/robots.txt fresh.' },
  { key: 'trai', label: 'TRAI', ids: 'trai_directions, trai_regulations, trai_consultations, trai_tariff_telecom, trai_tariff_broadcasting, trai_recommendations, trai_press_releases, trai_miscellaneous, trai_open_consultations, trai_standing_qos, trai_standing_broadband, trai_standing_bcs, trai_standing_fea, trai_standing_nsl, trai_whats_new, trai_rss',
    focus: 'trai.gov.in robots.txt permits our paths (52 rules, none covering release-publication); the Copyright Policy requires prior permission "by sending a mail" for REPRODUCTION (bites a PDF archive, not the metadata dashboard). Directions/regulations that are gazetted also get s.52(1)(q)(i). Distinguish metadata (EBC v Modak facts) from any document archive.' },
  { key: 'meity_pib', label: 'MeitY + PIB (candidate promotions)', ids: 'meity_gazettes, meity_acts_policies, meity_orders_notices, meity_guidelines (WP-JSON API), pib_moc (RSS)',
    focus: 'meity.gov.in robots.txt is "Allow: /" (express permission) but its WAF returns 403 to identified bots and 200 only to a browser UA — reason on whether presenting a browser string to a site that EXPRESSLY permits crawling in robots is legitimate, and record it as a qualified position. MeitY copyright policy is the permissive GoI clause ("without requiring specific permission"). PIB: no prior approval needed for reproduction; releases are ABOUT instruments (announcement layer).' },
  { key: 'certin_nccs_tec', label: 'CERT-In + NCCS + TEC', ids: 'certin_directions, certin_guidelines, certin_advisories, certin_vuln, nccs_latest, nccs_sas, nccs_sc, nccs_itsars, tec_circulars, tec_gazette_standards, tec_essential_requirements',
    focus: 'CERT-In: no usable robots.txt; Copyright Policy requires prior permission (reproduction). NCCS/TEC: no robots.txt (404); implied permission + RTI s.4 publication duty; TEC copyright conflicting across pages (treat reproduction as restricted). All statutory bodies publishing under duty. Gazette-notified TEC standards also get s.52(1)(q)(i).' },
  { key: 'media', label: 'MIB + CBFC + PRGI + ASCI', ids: 'mib_acts_policy, mib_advisories, mib_broadcasting, mib_orders_notices, mib_other_comms, mib_digital_media, cbfc, prgi, asci',
    focus: 'MIB robots.txt permits our paths (its /robots.txt intermittently 403s = unavailable = no restriction, RFC 9309); MIB/CBFC copyright "may not be reproduced without due permission". PRGI conflicting copyright. ASCI is a PRIVATE self-regulatory body — "All Rights Reserved", NO s.52(1)(q) limb available (no gazette/Act/judgment), so metadata-only is the only safe posture; reason carefully.' },
  { key: 'inspace_tdsat', label: 'IN-SPACe + TDSAT (satcom + tribunal)', ids: 'inspace_publications (ServiceNow API), tdsat_orders (POST judgments lane), tdsat_notices (signals)',
    focus: 'IN-SPACe: allow-list robots; reproduction free with acknowledgement. TDSAT: no robots.txt, no terms; s.52(1)(q)(iv) EXPRESSLY permits reproducing judgments/orders of a tribunal unless the tribunal prohibits (it does not) — the cleanest reproduction footing available. Distinguish tdsat_orders (judicial, q(iv)) from tdsat_notices (administrative court-diary, NOT q(iv), metadata-only).' },
  { key: 'new_consumer_competition', label: 'NEW: CCPA + CCI + DPIIT', ids: 'PROPOSE ids: ccpa_orders (doca.gov.in), cci_orders (cci.gov.in combination + antitrust orders), dpiit_press_notes (dpiit.gov.in FDI press notes)',
    focus: 'These are candidate ADDITIONS for a broad TMT practice. VERIFY each live (curl with browser UA): robots.txt for cci.gov.in, doca.gov.in, dpiit.gov.in; find and read their terms/copyright pages; check reachability of the orders/press-note listings. Reason on access (s.43) and reproduction (GoI works: s.52(1)(q) where gazetted, else metadata + s.52(1)(a)). CCI orders and DPIIT press notes are frequently gazetted. Recommend include/exclude honestly.' },
  { key: 'new_judgments', label: 'NEW: Supreme Court + Delhi HC + NCLAT', ids: 'PROPOSE ids: sc_judgments (main.sci.gov.in judgment/order lists), delhihc_judgments (delhihighcourt.nic.in), nclat_orders (nclat.nic.in)',
    focus: 'Candidate ADDITIONS, TMT-curated judgments. VERIFY each live: robots.txt and terms for main.sci.gov.in, delhihighcourt.nic.in, nclat.nic.in. Reproduction footing is strong: s.52(1)(q)(iv) expressly permits reproducing any judgment/order of a court/tribunal unless prohibited. Assess access (s.43): court sites, published for the public. Flag any that are captcha/anti-bot gated (do NOT recommend bypassing). Recommend only the ones both reachable and clean. No aggregators (LiveLaw/IndianKanoon out).' },
]

const results = await parallel(GROUPS.map(g => () => agent(
  'You are senior counsel producing a rigorous, defensible legal analysis for a top Indian law firm\'s TMT practice. Today is ' + TODAY + '. This will be handed to a partner as a per-source legal opinion memo, so precision, verbatim quotation and honesty about uncertainty matter more than reassurance.\n\n' +
  'Your source group: ' + g.label + '\nSources: ' + g.ids + '\nFocus for this group: ' + g.focus + '\n\n' +
  'Context you can read: ' + ROOT + '/engine/registry_v2.json (current source configs), ' + ROOT + '/engine/audit/legality_2026-08-25.json (an earlier robots/terms survey - use it but RE-VERIFY the robots.txt fresh yourself, terms change). Current date ' + TODAY + '.\n\n' +
  'For EACH source in your group, produce TWO INDEPENDENT legal analyses - they must be reasoned separately because a source can be lawful to access but not to reproduce, or vice versa:\n\n' +
  '(A) ACCESS / SCRAPING - under the Information Technology Act 2000. The tracker fetches public listing pages at ~1 request/second with an identifying User-Agent, no authentication bypassed, honouring robots.txt.\n' +
  '  - Fetch the live robots.txt yourself: curl -sL --max-time 20 -A "' + UA + '" https://HOST/robots.txt . Quote the relevant rules VERBATIM. Determine the verdict for the exact path we fetch under RFC 9309 (a 4xx/unavailable robots = no restriction; rules before any User-agent line bind nobody).\n' +
  '  - Find and read the terms of use / website policy. Quote VERBATIM any clause on automated access, crawling, scraping, harvesting, data mining, or "none found" (say which pages you checked).\n' +
  '  - Reason under s.43(a) (access) and s.43(b) (download/extract): is this "without permission of the owner"? Classify as EXPRESS permission (robots Allow / empty Disallow), IMPLIED permission (open publication at HTTP 200, no exclusion, plus the RTI Act 2005 s.4(1)(b)/(2) proactive-disclosure duty on public authorities), or REFUSED. Address the disruption limbs s.43(e)/(f) (rate/proportionality, s.47 factors incl. repetitiveness) and the s.66 mens rea ("dishonestly or fraudulently"). Note honestly that no Indian court has ruled on scraping publicly-available data and that browsewrap enforceability is unsettled.\n' +
  '  - Verdict: permissible / permissible_qualified / not_permissible, with the reason.\n\n' +
  '(B) REPRODUCTION / COPYRIGHT - under the Copyright Act 1957. The dashboard stores only bibliographic metadata (title, date, type, links); an OPTIONAL feature archives the official PDF privately for the firm.\n' +
  '  - Copyright owner: government work under s.17(d)? private body? court?\n' +
  '  - Which exception applies and to what: s.52(1)(q)(i) (matter published in the Official Gazette, EXCEPT an Act); s.52(1)(q)(ii) (an Act, only with commentary); s.52(1)(q)(iv) (judgment/order of a court or tribunal unless prohibited); s.52(1)(a) fair dealing for private use including research (per ANI Media v OpenAI, Delhi HC 24 Jul 2026: no non-commercial limit, "private" reaches a closed firm, lawyers researching to advise = research); EBC v Modak (no copyright in facts - the metadata dashboard needs no exception at all); an express reuse licence in the site\'s own copyright policy; or GODL. For the Gazette add IT Act s.8 and Bharatiya Sakshya Adhiniyam 2023 s.81 (presumption of genuineness of the electronic gazette record).\n' +
  '  - Distinguish clearly: the metadata dashboard (almost always clean on EBC v Modak) vs the private PDF archive (needs a real exception or licence).\n' +
  '  - Verdict: permissible / permissible_qualified / not_permissible.\n\n' +
  'Then an OVERALL recommendation per source: include / include_metadata_only / include_with_conditions / exclude - with conditions and residual risk stated plainly. If a source should be excluded, say so.\n\n' +
  'Never invent a clause or a robots rule. Quote what you actually fetch. Where the law is unsettled, say so rather than asserting a clean answer. Your final text is data for a pipeline that will render a PDF.',
  { label: 'legal:' + g.key, phase: 'Analyse', schema: SRC_SCHEMA })))

const all = results.filter(Boolean).flatMap(r => r.sources)
log('Analysed ' + all.length + ' sources; include=' + all.filter(s => s.overall !== 'exclude').length + ', exclude=' + all.filter(s => s.overall === 'exclude').length)

phase('Consolidate')

const FINAL_SCHEMA = {
  type: 'object', required: ['approved', 'excluded', 'principles', 'summary'],
  properties: {
    approved: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'authority', 'posture', 'access_one_line', 'copyright_one_line'],
        properties: {
          id: { type: 'string' }, authority: { type: 'string' },
          posture: { type: 'string', enum: ['include', 'include_metadata_only', 'include_with_conditions'] },
          access_one_line: { type: 'string' }, copyright_one_line: { type: 'string' }, conditions: { type: 'string' },
        },
      },
    },
    excluded: {
      type: 'array',
      items: { type: 'object', required: ['id', 'authority', 'why'], properties: { id: { type: 'string' }, authority: { type: 'string' }, why: { type: 'string' } } },
    },
    principles: { type: 'array', items: { type: 'string' } },
    new_sources_to_build: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}

const final = await agent(
  'You are the lead partner signing off a per-source legal analysis for a TMT regulatory tracker. Today is ' + TODAY + '.\n\n' +
  'Here are the per-source analyses from eight counsel (JSON):\n' + JSON.stringify(all) + '\n\n' +
  'Consolidate into the sign-off:\n' +
  '1. approved - every source cleared to ship, each with its posture (include / include_metadata_only / include_with_conditions) and a ONE-LINE access rationale and a ONE-LINE copyright rationale a partner can read at a glance. Include conditions where any.\n' +
  '2. excluded - sources that must NOT ship, with the reason (e.g. private body, rights reserved, no exception available; or a robots exclusion).\n' +
  '3. principles - the cross-cutting legal foundations stated once, cleanly, so the PDF can open with them: the s.43 "permission" spectrum (express/implied/refused) and that robots.txt is evidence not law; EBC v Modak (metadata = facts = no copyright); the s.52(1)(q) family (gazette/Act/judgment); s.52(1)(a) fair dealing as construed in ANI Media v OpenAI; the Gazette\'s special standing (IT Act s.8, BSA 2023 s.81); and the genuinely unsettled questions (scraping of public data untested in India; browsewrap enforceability).\n' +
  '4. new_sources_to_build - the cleared candidate additions (CCPA/CCI/DPIIT/judgments) that should be built, with host.\n' +
  '5. summary - 4-5 sentences a partner reads first.\n\n' +
  'Be rigorous and consistent; if two counsel reasoned a shared principle differently, resolve it. Your final text is data for a PDF renderer.',
  { label: 'signoff', phase: 'Consolidate', schema: FINAL_SCHEMA })

return { per_source: all, signoff: final }
