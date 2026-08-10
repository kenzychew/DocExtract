const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const submitBtn = document.getElementById("submit-btn");
const statusEl = document.getElementById("status");
const pageEl = document.querySelector(".page");
const workspaceEl = document.getElementById("workspace");
const previewPanel = document.getElementById("preview-panel");
const previewContent = document.getElementById("preview-content");
const resultEl = document.getElementById("result");
const verdictEl = document.getElementById("verdict");
const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanels = {
  fields: document.getElementById("tab-fields"),
  checks: document.getElementById("tab-checks"),
  details: document.getElementById("tab-details"),
};

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    tabButtons.forEach((b) => b.classList.remove("active"));
    Object.values(tabPanels).forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    tabPanels[btn.dataset.tab].classList.add("active");
  });
});

function setStatus(message, isError) {
  statusEl.hidden = !message;
  statusEl.textContent = message || "";
  statusEl.classList.toggle("error", Boolean(isError));
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value === null || value === undefined ? "" : String(value);
  return div.innerHTML;
}

function fmtMoney(value, currency) {
  if (value === null || value === undefined) return "-";
  const prefix = currency ? `${currency} ` : "";
  return `${prefix}${Number(value).toFixed(2)}`;
}

function fmtConf(value) {
  if (value === null || value === undefined) return "-";
  return `${Math.round(value * 100)}%`;
}

const IMAGE_EXT_RE = /\.(jpe?g|png|webp|gif|bmp)$/i;
const IMAGE_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
  "image/bmp",
]);

let currentPreviewUrl = null;

function clearPreview() {
  if (currentPreviewUrl) {
    URL.revokeObjectURL(currentPreviewUrl);
    currentPreviewUrl = null;
  }
  previewContent.innerHTML = "";
  previewPanel.hidden = true;
  pageEl.classList.remove("has-preview");
  workspaceEl.classList.remove("has-preview");
}

function showFallback(file, message) {
  const fallback = document.createElement("div");
  fallback.className = "preview-fallback";
  const nameEl = document.createElement("strong");
  nameEl.textContent = file.name;
  const messageEl = document.createElement("p");
  messageEl.style.margin = "0";
  messageEl.textContent = message;
  fallback.appendChild(nameEl);
  fallback.appendChild(messageEl);
  previewContent.appendChild(fallback);
}

function showPreview(file) {
  clearPreview();
  if (!file) return;

  previewPanel.hidden = false;
  pageEl.classList.add("has-preview");
  workspaceEl.classList.add("has-preview");

  const name = file.name || "";
  const isTiff = file.type === "image/tiff" || /\.tiff?$/i.test(name);
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(name);
  const isImage = !isTiff && (IMAGE_MIME_TYPES.has(file.type) || IMAGE_EXT_RE.test(name));

  if (isTiff) {
    showFallback(file, "Inline preview isn't available for TIFF in most browsers, but the file will still be sent for extraction.");
    return;
  }

  currentPreviewUrl = URL.createObjectURL(file);

  if (isPdf) {
    const iframe = document.createElement("iframe");
    iframe.src = currentPreviewUrl;
    iframe.title = "Document preview";
    previewContent.appendChild(iframe);
    return;
  }

  if (isImage) {
    const img = document.createElement("img");
    img.src = currentPreviewUrl;
    img.alt = `Preview of ${name}`;
    previewContent.appendChild(img);
    return;
  }

  showFallback(file, "Inline preview isn't available for this file type.");
}

fileInput.addEventListener("change", () => {
  showPreview(fileInput.files[0] || null);
});

function renderVerdict(payload) {
  const isAccept = payload.decision === "accept";
  verdictEl.className = `verdict ${isAccept ? "accept" : "review"}`;

  if (isAccept) {
    verdictEl.innerHTML = `
      <h2>Accepted automatically</h2>
      <p>Every must-pass check cleared and confidence was high enough to write this
      document straight through with no human step.</p>
    `;
    return;
  }

  const doc = payload.document;
  const hardFailures = (doc.validation.results || []).filter(
    (r) => r.severity === "hard" && r.status === "fail"
  );

  let reason;
  if (payload.error) {
    reason = `Something went wrong while reading this document, so it was sent to review instead of being accepted. (${escapeHtml(payload.error)})`;
  } else if (hardFailures.length === 1) {
    reason = escapeHtml(hardFailures[0].message);
  } else if (hardFailures.length > 1) {
    reason = "More than one must-pass check failed: " + hardFailures.map((r) => escapeHtml(r.message)).join("; ");
  } else {
    reason = `Every must-pass check cleared, but confidence came out at ${fmtConf(payload.confidence)}, below the ${fmtConf(payload.threshold)} needed to accept automatically.`;
  }

  verdictEl.innerHTML = `
    <h2>Sent to review</h2>
    <p>${reason}</p>
    <p>Review is the safe default here, not a failure -- the extracted fields are below for a person to confirm.</p>
  `;
}

function renderFields(doc) {
  const currency = doc.currency;
  const rows = [
    ["Type", doc.doc_type, doc.field_confidence.doc_type],
    ["Vendor", doc.vendor_name, doc.field_confidence.vendor_name],
    ["Address", doc.vendor_address, doc.field_confidence.vendor_address],
    ["Invoice No.", doc.invoice_number, doc.field_confidence.invoice_number],
    ["Date", doc.document_date, doc.field_confidence.document_date],
    ["Due date", doc.due_date, doc.field_confidence.due_date],
    ["Currency", doc.currency, doc.field_confidence.currency],
    ["Subtotal", fmtMoney(doc.subtotal, currency), doc.field_confidence.subtotal],
    ["Tax", fmtMoney(doc.tax, currency), doc.field_confidence.tax],
    ["Total", fmtMoney(doc.total, currency), doc.field_confidence.total],
    ["Line items", doc.line_items.length, null],
  ];

  let html = "<table><thead><tr><th>Field</th><th>Value</th><th>Confidence</th></tr></thead><tbody>";
  for (const [label, value, conf] of rows) {
    html += `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(value ?? "-")}</td><td>${fmtConf(conf)}</td></tr>`;
  }
  html += "</tbody></table>";

  if (doc.line_items.length) {
    html += "<h3>Line items</h3><table><thead><tr><th>#</th><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>";
    doc.line_items.forEach((item, i) => {
      html += `<tr><td>${i + 1}</td><td>${escapeHtml(item.description ?? "-")}</td><td>${escapeHtml(item.quantity ?? "-")}</td><td>${fmtMoney(item.unit_price, currency)}</td><td>${fmtMoney(item.amount, currency)}</td></tr>`;
    });
    html += "</tbody></table>";
  }

  tabPanels.fields.innerHTML = html;
}

const STATUS_LABEL = { pass: "Passed", fail: "Failed", skip: "Not applicable" };
const STATUS_CLASS = { pass: "check-pass", fail: "check-fail", skip: "check-skip" };

function renderChecks(doc) {
  const results = doc.validation.results || [];
  const groups = [
    ["hard", "Must pass to auto-accept"],
    ["soft", "Quality signals"],
  ];

  let html = "";
  for (const [severity, heading] of groups) {
    const rules = results.filter((r) => r.severity === severity);
    if (!rules.length) continue;
    html += `<div class="checks-group"><h3>${heading}</h3>`;
    for (const rule of rules) {
      const cls = STATUS_CLASS[rule.status] || "";
      const label = STATUS_LABEL[rule.status] || rule.status;
      html += `<div class="check-line ${cls}"><strong>${label}</strong> -- ${escapeHtml(rule.message)}</div>`;
    }
    html += "</div>";
  }

  tabPanels.checks.innerHTML = html || "<p>No checks ran.</p>";
}

function renderDetails(payload) {
  const doc = payload.document;
  const lines = [
    `Decision: ${payload.decision}`,
    `Confidence: ${fmtConf(payload.confidence)} (auto-accept threshold: ${fmtConf(payload.threshold)})`,
    `Backend: ${payload.backend}`,
    `Modality: ${payload.modality || "unknown"}`,
  ];
  if (payload.error) lines.push(`Pipeline error: ${payload.error}`);

  const html =
    `<div class="details-list">` +
    lines.map((l) => `<div>${escapeHtml(l)}</div>`).join("") +
    `</div>` +
    `<table><thead><tr><th>Rule</th><th>Severity</th><th>Status</th><th>Message</th></tr></thead><tbody>` +
    (doc.validation.results || [])
      .map(
        (r) =>
          `<tr><td>${r.code}</td><td>${r.severity}</td><td>${r.status}</td><td>${escapeHtml(r.message)}</td></tr>`
      )
      .join("") +
    `</tbody></table>`;

  tabPanels.details.innerHTML = html;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  submitBtn.disabled = true;
  resultEl.hidden = true;
  workspaceEl.classList.remove("has-result");
  setStatus("Extracting... this can take up to a minute.", false);

  const formData = new FormData();
  formData.append("file", file);

  try {
    const response = await fetch("/api/extract", { method: "POST", body: formData });
    const payload = await response.json().catch(() => null);

    if (!response.ok) {
      const detail = (payload && payload.detail) || `Request failed (${response.status}).`;
      setStatus(detail, true);
      return;
    }

    setStatus("", false);
    renderVerdict(payload);
    renderFields(payload.document);
    renderChecks(payload.document);
    renderDetails(payload);
    resultEl.hidden = false;
    workspaceEl.classList.add("has-result");
  } catch (err) {
    setStatus(`Network error: ${err.message}`, true);
  } finally {
    submitBtn.disabled = false;
  }
});
