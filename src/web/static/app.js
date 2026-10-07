const state = { household: null, pendingLastFour: null, pendingRefund: null, pendingMapping: null, pendingReview: null };
const tabs = [...document.querySelectorAll('[role="tab"]')];
const feedback = document.querySelector('#feedback');

const displayId = value => String(value).replaceAll('_', ' ').replaceAll('-', ' ').replace(/\b\w/g, char => char.toUpperCase());
const cents = value => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value / 100);
function setFeedback(message, isError = false) { feedback.textContent = message; feedback.classList.toggle('error', isError); feedback.hidden = !message; }
function setActiveTab(tab) { tabs.forEach(candidate => { const selected = candidate === tab; candidate.setAttribute('aria-selected', String(selected)); document.querySelector(`#${candidate.getAttribute('aria-controls')}`).classList.toggle('active', selected); }); if (tab.id === 'past-tab') loadHistory(); }
function selectedVendors() { return [...document.querySelectorAll('input[name="vendor"]:checked')].map(input => input.value); }

async function api(path, options = {}) { const response = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options }); const payload = await response.json().catch(() => ({})); if (!response.ok) { const error = new Error(payload.detail || 'The local service could not complete that request.'); error.payload = payload; throw error; } return payload; }
function fillPayerSelects() { const participants = state.household?.participant_ids || []; for (const select of document.querySelectorAll('.payer-select')) select.replaceChildren(...participants.map(id => new Option(displayId(id), id))); }
function renderHousehold(household) {
  const { correlation_id: ignoredCorrelationId, ...config } = household;
  state.household = config;
  fillPayerSelects();
  const target = document.querySelector('#mapping-list');
  const mappings = config.payment_mappings || [];
  target.replaceChildren(...mappings.map(mapping => {
    const row = document.createElement('div');
    row.className = 'mapping';
    const payer = document.createElement('span');
    payer.textContent = displayId(mapping.payer_id);
    const card = document.createElement('span');
    card.className = 'card-brand';
    const brand = mapping.payment_method ? displayId(mapping.payment_method) : 'Card';
    card.textContent = `${brand} · ${mapping.last_four}`;
    row.append(payer, card);
    return row;
  }));
  const absenceSelect = document.querySelector('#absence-participant');
  if (absenceSelect) {
    absenceSelect.replaceChildren(...(config.participant_ids || []).map(id => new Option(displayId(id), id)));
  }
  const absenceTarget = document.querySelector('#absence-list');
  if (absenceTarget) {
    const absences = config.absences || [];
    absenceTarget.replaceChildren(...absences.map(absence => {
      const row = document.createElement('div');
      row.className = 'mapping';
      const text = document.createElement('span');
      text.textContent = `${displayId(absence.participant_id)} · ${absence.starts_on} → ${absence.ends_on}`;
      row.append(text);
      return row;
    }));
  }
  document.querySelector('#raw-json').value = JSON.stringify(config, null, 2);
}
async function loadHousehold() { try { renderHousehold(await api('/api/v1/household')); } catch { setFeedback('Household configuration is unavailable. Check the local setup and logs.', true); } }
function createSummarySection(titleText, summary, className = '') { const section = document.createElement('section'); section.className = `summary ${className}`.trim(); const header = document.createElement('div'); header.className = 'summary-head'; const title = document.createElement('strong'); title.textContent = titleText; const actions = document.createElement('div'); if (summary.includes('Refund adjustment:')) { const badge = document.createElement('span'); badge.className = 'badge refund-badge'; badge.textContent = 'Refund adjusted'; actions.append(badge); } const copy = document.createElement('button'); copy.className = 'secondary'; copy.type = 'button'; copy.textContent = 'Copy summary'; copy.addEventListener('click', async () => { await navigator.clipboard.writeText(summary); setFeedback('Copy-ready summary copied to your clipboard.'); }); actions.append(copy); header.append(title, actions); const body = document.createElement('pre'); body.textContent = summary; section.append(header, body); return section; }
function renderSummaries(aggregateSummary, summaries, payerAggregates = null) { const target = document.querySelector('#summaries'); const payerSections = []; if (payerAggregates && typeof payerAggregates === 'object') { for (const [payerId, payerData] of Object.entries(payerAggregates)) { const payerName = displayId(payerId); const title = `Payer total · Paid by ${payerName} (${cents(payerData.total_cents)})`; payerSections.push(createSummarySection(title, payerData.markdown_summary || '', 'payer-total')); } } const receiptSections = summaries.map((summary, index) => createSummarySection(`Receipt ${index + 1}`, summary)); target.replaceChildren(createSummarySection('Run total · all receipts', aggregateSummary, 'run-total'), ...payerSections, ...receiptSections); document.querySelector('#review-state').hidden = false; }
function unknownPaymentDescription(source) { const payment = source.payment_method ? displayId(source.payment_method) : 'This payment method'; return source.last_four ? `${payment} ending in ${source.last_four} is not mapped. Select its payer for this run.` : `${payment} has no card suffix. Select its payer for this run.`; }
function renderUnknownPaymentDetails(source) { const payment = source.payment_method ? displayId(source.payment_method) : 'Unknown'; const paymentDetail = source.last_four ? `${payment} ending in ${source.last_four}` : `${payment} with no card suffix`; const details = [['Retailer', displayId(source.retailer || 'Unknown')], ['Purchase date', source.purchase_date || 'Not available'], ['Receipt ID', source.receipt_id || 'Not available'], ...(source.amount_cents !== undefined && source.amount_cents !== null ? [['Amount', cents(source.amount_cents)]] : []), ['Payment detected', paymentDetail], ['Source email', source.email_sender && source.email_subject ? `${source.email_sender} — ${source.email_subject}` : 'Not available']]; const target = document.querySelector('#resolution-details-list'); target.replaceChildren(...details.flatMap(([label, value]) => { const term = document.createElement('dt'); term.textContent = label; const definition = document.createElement('dd'); definition.textContent = value; return [term, definition]; })); }
function clearPayerResolution() { state.pendingLastFour = null; state.pendingMapping = null; document.querySelector('#mapping-resolution').hidden = true; }
function clearRefundResolution() { state.pendingRefund = null; const card = document.querySelector('#refund-resolution'); if (card) card.hidden = true; }
function clearReviewResolution() { state.pendingReview = null; const card = document.querySelector('#review-resolution'); if (card) card.hidden = true; }
function renderReviewResolution(source) { const card = document.querySelector('#review-resolution'); if (!card) return; document.querySelector('#review-resolution-description').textContent = receiptReviewMessage(source); const details = [['Retailer', displayId(source.retailer || 'Unknown')], ['Reason', displayId(source.reason_code || 'Review required')], ['Message ID', source.message_id || 'Not available'], ['Failure detail', source.failure_detail || 'None reported'], ['Source email', source.email_sender && source.email_subject ? `${source.email_sender} — ${source.email_subject}` : 'Not available']]; const target = document.querySelector('#review-details-list'); target.replaceChildren(...details.flatMap(([label, value]) => { const term = document.createElement('dt'); term.textContent = label; const definition = document.createElement('dd'); definition.textContent = value; return [term, definition]; })); card.hidden = false; }
function renderRefundDetails(refund) { const details = [['Refund ID', refund.refund_id || 'Not available'], ['Date', refund.refund_date || 'Not available'], ['Amount', cents(refund.refund_amount_cents || 0)], ['Retailer', displayId(refund.retailer || 'Unknown')]]; const target = document.querySelector('#refund-details-list'); target.replaceChildren(...details.flatMap(([label, value]) => { const term = document.createElement('dt'); term.textContent = label; const definition = document.createElement('dd'); definition.textContent = value; return [term, definition]; })); }
function fillRefundCandidates(candidates) { const select = document.querySelector('#refund-candidate'); select.replaceChildren(...candidates.map(candidate => { const label = `${candidate.purchase_date} · ${candidate.receipt_id} · ${cents(candidate.final_total_cents)}`; return new Option(label, candidate.receipt_id); })); }
function receiptReviewMessage(source) { const retailer = displayId(source.retailer || 'Unknown retailer'); const sourceEmail = source.email_sender && source.email_subject ? ` Source: ${source.email_sender} — ${source.email_subject}.` : ''; return `${source.detail} ${retailer} reported ${displayId(source.reason_code || 'a processing error')}.${sourceEmail} Your payer choice was accepted; adjust the date range to exclude this unsupported receipt, or mark it for manual entry.`; }
function receiptSourceDescription(source) { return source === 'local_cache' ? 'using the local receipt cache; Gmail was not queried' : 'from Gmail and cached locally for later retries'; }
async function runReceipts(manualPayerId = null) { const vendors = selectedVendors(); if (!vendors.length) { setFeedback('Select at least one receipt source.', true); return; } const payload = { start_on: document.querySelector('#start-on').value, end_on: document.querySelector('#end-on').value, vendors }; if (manualPayerId) payload.manual_payer_id = manualPayerId; const submit = document.querySelector('#run-form button[type="submit"]'); submit.disabled = true; setFeedback('Loading receipt records locally or fetching them once from Gmail…'); try { const result = await api('/api/v1/runs', { method: 'POST', body: JSON.stringify(payload) }); clearPayerResolution(); clearRefundResolution(); clearReviewResolution(); const manualText = result.manual_count > 0 ? ` ${result.manual_count} marked for manual entry.` : ''; document.querySelector('#review-description').textContent = `${result.automated_count} of ${result.total_count} receipts automated successfully.${manualText} Run total: ${cents(result.aggregate_total_cents)}.`; renderSummaries(result.aggregate_summary, result.summaries, result.payer_aggregates); setFeedback(`Receipt run completed ${receiptSourceDescription(result.receipt_source)}. Nothing was posted to Splitwise.`); loadHistory(); } catch (error) { if (error.payload?.last_four !== undefined) { clearRefundResolution(); clearReviewResolution(); state.pendingLastFour = error.payload.last_four; state.pendingMapping = error.payload; document.querySelector('#mapping-description').textContent = unknownPaymentDescription(error.payload); renderUnknownPaymentDetails(error.payload); document.querySelector('#mapping-resolution').hidden = false; fillPayerSelects(); setFeedback(`A receipt needs your payer decision before splitting; it was loaded ${receiptSourceDescription(error.payload.receipt_source)}.`, true); } else if (error.payload?.reason_code === 'refund_receipt_link_required') { clearPayerResolution(); clearReviewResolution(); state.pendingRefund = error.payload.refund; renderRefundDetails(error.payload.refund || {}); fillRefundCandidates(error.payload.candidates || []); document.querySelector('#refund-resolution').hidden = false; setFeedback(`A refund needs an original receipt selection before this run can finish; it was loaded ${receiptSourceDescription(error.payload.receipt_source)}.`, true); } else if (error.payload?.reason_code === 'gmail_authorization_failed') { clearPayerResolution(); clearRefundResolution(); clearReviewResolution(); setFeedback(error.payload.detail || 'Gmail authorization expired and was removed. Click Fetch and review receipts to complete browser sign-in.', true); } else if (error.payload?.reason_code) { clearPayerResolution(); clearRefundResolution(); state.pendingReview = error.payload; renderReviewResolution(error.payload); setFeedback(receiptReviewMessage(error.payload), true); } else setFeedback(error.message, true); } finally { submit.disabled = false; } }
async function loadHistory() { try { const history = await api('/api/v1/runs'); const target = document.querySelector('#past-runs'); if (!history.runs.length) { target.textContent = 'No saved receipt results yet.'; target.className = 'empty-state'; return; } target.className = ''; target.replaceChildren(...history.runs.map(run => { const row = document.createElement('div'); row.className = 'job'; const left = document.createElement('div'); const title = document.createElement('strong'); title.textContent = `${displayId(run.retailer)} · ${run.receipt_id}`; const detail = document.createElement('p'); detail.className = 'muted'; detail.textContent = `${run.purchase_date} · paid by ${displayId(run.payer_id)}`; left.append(title, detail); const right = document.createElement('div'); right.className = 'amount'; right.innerHTML = `<span class="badge">Saved</span><br><strong>${cents(run.final_total_cents)}</strong>`; row.append(left, right); return row; })); } catch { setFeedback('Past transactions could not be loaded. Check the local logs.', true); } }

tabs.forEach(tab => tab.addEventListener('click', () => setActiveTab(tab)));
document.querySelector('#run-form').addEventListener('submit', event => { event.preventDefault(); runReceipts(); });
document.querySelector('#mapping-resolution-form').addEventListener('submit', async event => {
  event.preventDefault();
  const payerId = document.querySelector('#unknown-payer').value;
  try {
    if (state.pendingLastFour) {
      await api('/api/v1/household/payment-mappings', { method: 'POST', body: JSON.stringify({ last_four: state.pendingLastFour, payer_id: payerId }) });
    } else if (state.pendingMapping?.receipt_id) {
      await api('/api/v1/household/receipt-payer-mappings', { method: 'POST', body: JSON.stringify({ receipt_id: state.pendingMapping.receipt_id, payer_id: payerId }) });
    }
    await loadHousehold();
    await runReceipts();
  } catch (error) {
    setFeedback(error.message, true);
  }
});
const mappingManualBtn = document.querySelector('#mapping-manual-btn');
if (mappingManualBtn) {
  mappingManualBtn.addEventListener('click', async () => {
    const messageId = state.pendingMapping?.message_id || state.pendingMapping?.receipt_id;
    if (!messageId) return;
    try {
      const payload = {
        message_id: messageId,
        reason: 'unknown_payment_mapping',
        metadata: {
          retailer: state.pendingMapping.retailer,
          last_four: state.pendingMapping.last_four,
          payment_method: state.pendingMapping.payment_method,
        },
        start_on: document.querySelector('#start-on').value,
        end_on: document.querySelector('#end-on').value,
        vendors: selectedVendors(),
      };
      clearPayerResolution();
      await api('/api/v1/manual-receipts', { method: 'POST', body: JSON.stringify(payload) });
      await runReceipts();
    } catch (error) {
      setFeedback(error.message, true);
    }
  });
}
const reviewManualBtn = document.querySelector('#review-manual-btn');
if (reviewManualBtn) {
  reviewManualBtn.addEventListener('click', async () => {
    const messageId = state.pendingReview?.message_id;
    if (!messageId) return;
    try {
      const payload = {
        message_id: messageId,
        reason: state.pendingReview.reason_code || 'manual_review_required',
        metadata: {
          retailer: state.pendingReview.retailer,
          reason_code: state.pendingReview.reason_code,
          failure_detail: state.pendingReview.failure_detail,
        },
        start_on: document.querySelector('#start-on').value,
        end_on: document.querySelector('#end-on').value,
        vendors: selectedVendors(),
      };
      clearReviewResolution();
      await api('/api/v1/manual-receipts', { method: 'POST', body: JSON.stringify(payload) });
      await runReceipts();
    } catch (error) {
      setFeedback(error.message, true);
    }
  });
}
document.querySelector('#refund-resolution-form').addEventListener('submit', async event => { event.preventDefault(); if (!state.pendingRefund) return; const originalReceiptId = document.querySelector('#refund-candidate').value; const payload = { refund_id: state.pendingRefund.refund_id, original_receipt_id: originalReceiptId, refund_amount_cents: state.pendingRefund.refund_amount_cents }; if (state.pendingRefund.refund_date) payload.refund_date = state.pendingRefund.refund_date; try { await api('/api/v1/refund-links', { method: 'POST', body: JSON.stringify(payload) }); clearRefundResolution(); await runReceipts(); } catch (error) { setFeedback(error.message, true); } });
document.querySelector('#mapping-form').addEventListener('submit', async event => { event.preventDefault(); try { await api('/api/v1/household/payment-mappings', { method: 'POST', body: JSON.stringify({ last_four: document.querySelector('#last-four').value, payer_id: document.querySelector('#payer').value }) }); event.target.reset(); await loadHousehold(); setFeedback('Payment mapping saved locally.'); } catch (error) { setFeedback(error.message, true); } });
document.querySelector('#participant-form').addEventListener('submit', async event => { event.preventDefault(); const rawName = document.querySelector('#participant-name').value.trim(); const participantId = rawName.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''); try { await api('/api/v1/household/participants', { method: 'POST', body: JSON.stringify({ participant_id: participantId }) }); event.target.reset(); await loadHousehold(); setFeedback(`Added ${displayId(participantId)} to this local household.`); } catch (error) { setFeedback(error.message, true); } });
const absenceForm = document.querySelector('#absence-form');
if (absenceForm) {
  absenceForm.addEventListener('submit', async event => {
    event.preventDefault();
    const participantId = document.querySelector('#absence-participant').value;
    const startsOn = document.querySelector('#absence-start-on').value;
    const endsOn = document.querySelector('#absence-end-on').value;
    try {
      await api('/api/v1/household/absences', {
        method: 'POST',
        body: JSON.stringify({
          participant_id: participantId,
          starts_on: startsOn,
          ends_on: endsOn,
        }),
      });
      event.target.reset();
      await loadHousehold();
      setFeedback(`Absence window saved for ${displayId(participantId)}.`);
    } catch (error) {
      setFeedback(error.message, true);
    }
  });
}
const dialog = document.querySelector('#json-dialog'); document.querySelector('#open-json').addEventListener('click', () => dialog.showModal()); document.querySelector('#json-form').addEventListener('submit', async event => { if (event.submitter?.value === 'cancel') return; event.preventDefault(); try { const config = JSON.parse(document.querySelector('#raw-json').value); await api('/api/v1/household', { method: 'PUT', body: JSON.stringify(config) }); await loadHousehold(); dialog.close(); setFeedback('Validated household JSON saved locally.'); } catch (error) { setFeedback(error instanceof SyntaxError ? 'Raw household JSON is not valid JSON.' : error.message, true); } });
document.querySelector('#refresh-history').addEventListener('click', loadHistory);
const today = new Date().toISOString().slice(0, 10); document.querySelector('#end-on').value = today; const start = new Date(); start.setDate(start.getDate() - 30); document.querySelector('#start-on').value = start.toISOString().slice(0, 10);
const clearCacheBtn = document.querySelector('#clear-cache');
if (clearCacheBtn) {
  clearCacheBtn.addEventListener('click', async () => {
    try {
      const response = await api('/api/v1/cache/clear', { method: 'POST' });
      setFeedback(`Cache cleared: ${response.cleared_count} cached ingestion(s) removed.`);
    } catch (error) {
      setFeedback(error.message, true);
    }
  });
}
const resetOAuthBtn = document.querySelector('#reset-oauth-btn');
if (resetOAuthBtn) {
  resetOAuthBtn.addEventListener('click', async () => {
    try {
      await api('/api/v1/oauth/reset', { method: 'POST' });
      setFeedback('Saved Gmail login token was removed. The next receipt fetch will prompt for browser sign-in.');
    } catch (error) {
      setFeedback(error.message, true);
    }
  });
}
loadHousehold();


