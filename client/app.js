const CLIENT_CONFIG = window.VEPAY_API_CLIENT_CONFIG || {};
const rawApiPrefix =
  typeof CLIENT_CONFIG.apiPrefix === "string" ? CLIENT_CONFIG.apiPrefix : "/api";
const API_PREFIX = rawApiPrefix.replace(/\/$/, "");
const REQUIRE_API_KEY = Boolean(CLIENT_CONFIG.requireApiKey);
const state = {
  capabilities: null,
  files: [],
  lastResponse: null,
  busy: false,
};

const els = {
  serviceStatus: document.querySelector("#serviceStatus"),
  serviceStatusText: document.querySelector("#serviceStatusText"),
  fileInput: document.querySelector("#fileInput"),
  dropZone: document.querySelector("#dropZone"),
  limitText: document.querySelector("#limitText"),
  langInput: document.querySelector("#langInput"),
  apiKeyField: document.querySelector("#apiKeyField"),
  apiKeyInput: document.querySelector("#apiKeyInput"),
  enableCropsInput: document.querySelector("#enableCropsInput"),
  includeRawTextInput: document.querySelector("#includeRawTextInput"),
  clearFilesButton: document.querySelector("#clearFilesButton"),
  submitButton: document.querySelector("#submitButton"),
  fileList: document.querySelector("#fileList"),
  formMessage: document.querySelector("#formMessage"),
  metricTotal: document.querySelector("#metricTotal"),
  metricComplete: document.querySelector("#metricComplete"),
  metricIncomplete: document.querySelector("#metricIncomplete"),
  metricErrors: document.querySelector("#metricErrors"),
  metricDuration: document.querySelector("#metricDuration"),
  receiptsList: document.querySelector("#receiptsList"),
  errorsList: document.querySelector("#errorsList"),
  jsonOutput: document.querySelector("#jsonOutput"),
  copyJsonButton: document.querySelector("#copyJsonButton"),
  downloadJsonButton: document.querySelector("#downloadJsonButton"),
};

function setServiceStatus(kind, text) {
  const dot = els.serviceStatus.querySelector(".status-dot");
  dot.className = `status-dot status-dot--${kind}`;
  els.serviceStatusText.textContent = text;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function allowedExtensions() {
  return new Set(state.capabilities?.image_extensions || [".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"]);
}

function limits() {
  return {
    maxFiles: state.capabilities?.limits?.max_files || 10,
    maxFileSize: state.capabilities?.limits?.max_file_size_bytes || 10 * 1024 * 1024,
  };
}

function fileExtension(file) {
  const index = file.name.lastIndexOf(".");
  return index >= 0 ? file.name.slice(index).toLowerCase() : "";
}

function validationProblem(file) {
  const { maxFileSize } = limits();
  if (!allowedExtensions().has(fileExtension(file))) {
    return "Extension no soportada";
  }
  if (file.size > maxFileSize) {
    return `Supera ${formatBytes(maxFileSize)}`;
  }
  return "";
}

function refreshLimitText() {
  const { maxFiles, maxFileSize } = limits();
  const extensions = Array.from(allowedExtensions()).join(", ");
  els.limitText.textContent = `${maxFiles} archivos - ${formatBytes(maxFileSize)} c/u - ${extensions}`;
}

function selectedValidFiles() {
  return state.files.filter((file) => !validationProblem(file));
}

function updateSubmitState() {
  const { maxFiles } = limits();
  const validCount = selectedValidFiles().length;
  const hasTooMany = state.files.length > maxFiles;
  const missingApiKey = REQUIRE_API_KEY && validCount > 0 && !apiKeyValue();
  els.clearFilesButton.disabled = state.files.length === 0 || state.busy;
  els.submitButton.disabled = state.busy || validCount === 0 || hasTooMany || missingApiKey;
  if (state.busy) return;
  if (missingApiKey) {
    setFormMessage("API key requerida", true);
  } else if (hasTooMany) {
    setFormMessage(`Maximo ${maxFiles} archivos por solicitud`, true);
  } else if (state.files.length && validCount === 0) {
    setFormMessage("No hay archivos validos", true);
  } else if (!state.files.length) {
    setFormMessage("");
  } else {
    setFormMessage(`${validCount} archivo${validCount === 1 ? "" : "s"} listo${validCount === 1 ? "" : "s"}`);
  }
}

function setFormMessage(text, isError = false) {
  els.formMessage.textContent = text;
  els.formMessage.classList.toggle("error", Boolean(isError));
}

function apiKeyValue() {
  return els.apiKeyInput?.value.trim() || "";
}

function requestHeaders() {
  const headers = {};
  const apiKey = apiKeyValue();
  if (apiKey) {
    headers["X-API-Key"] = apiKey;
  }
  return headers;
}

function renderFiles() {
  if (!state.files.length) {
    els.fileList.className = "file-list empty-state";
    els.fileList.textContent = "Sin archivos seleccionados";
    updateSubmitState();
    return;
  }

  els.fileList.className = "file-list";
  els.fileList.replaceChildren(
    ...state.files.map((file, index) => {
      const item = document.createElement("div");
      item.className = "file-item";

      const img = document.createElement("img");
      img.className = "file-thumb";
      img.alt = "";
      img.src = URL.createObjectURL(file);
      img.onload = () => URL.revokeObjectURL(img.src);

      const body = document.createElement("div");
      const name = document.createElement("p");
      name.className = "file-name";
      name.textContent = file.name;
      const meta = document.createElement("p");
      meta.className = "file-meta";
      meta.textContent = `${file.type || "image/*"} - ${formatBytes(file.size)}`;
      body.append(name, meta);

      const problem = validationProblem(file);
      if (problem) {
        const issue = document.createElement("p");
        issue.className = "file-problem";
        issue.textContent = problem;
        body.append(issue);
      }

      const remove = document.createElement("button");
      remove.className = "remove-file-button";
      remove.type = "button";
      remove.textContent = "x";
      remove.title = "Quitar";
      remove.addEventListener("click", () => {
        state.files.splice(index, 1);
        renderFiles();
      });

      item.append(img, body, remove);
      return item;
    })
  );
  updateSubmitState();
}

function addFiles(fileList) {
  state.files.push(...Array.from(fileList));
  renderFiles();
}

function resetResults() {
  state.lastResponse = null;
  els.metricTotal.textContent = "0";
  els.metricComplete.textContent = "0";
  els.metricIncomplete.textContent = "0";
  els.metricErrors.textContent = "0";
  els.metricDuration.textContent = "-";
  els.receiptsList.className = "receipt-list empty-state";
  els.receiptsList.textContent = "Sin resultados";
  els.errorsList.className = "error-list empty-state";
  els.errorsList.textContent = "Sin errores";
  els.jsonOutput.textContent = "{}";
  els.copyJsonButton.disabled = true;
  els.downloadJsonButton.disabled = true;
}

function valueAt(object, path, fallback = "-") {
  const value = path.split(".").reduce((current, key) => current?.[key], object);
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function renderDataField(label, value, extraClass = "") {
  const field = document.createElement("div");
  field.className = `data-field ${extraClass}`.trim();
  const labelNode = document.createElement("span");
  labelNode.textContent = label;
  const valueNode = document.createElement("strong");
  valueNode.textContent = value || "-";
  field.append(labelNode, valueNode);
  return field;
}

function renderReceipts(receipts) {
  if (!receipts.length) {
    els.receiptsList.className = "receipt-list empty-state";
    els.receiptsList.textContent = "Sin recibos";
    return;
  }

  els.receiptsList.className = "receipt-list";
  els.receiptsList.replaceChildren(
    ...receipts.map((receipt, index) => {
      const complete = Boolean(receipt.validation?.is_complete);
      const card = document.createElement("article");
      card.className = `receipt-card ${complete ? "" : "incomplete"}`.trim();

      const header = document.createElement("div");
      header.className = "receipt-header";
      const title = document.createElement("div");
      title.className = "receipt-title";
      const h3 = document.createElement("h3");
      h3.textContent = valueAt(receipt, "source.file_name", `Recibo ${index + 1}`);
      const subtitle = document.createElement("p");
      subtitle.textContent = valueAt(receipt, "source.file_path");
      title.append(h3, subtitle);
      const badge = document.createElement("span");
      badge.className = `status-badge ${complete ? "complete" : "incomplete"}`;
      badge.textContent = complete ? "Completo" : "Revision";
      header.append(title, badge);

      const fields = document.createElement("div");
      fields.className = "receipt-fields";
      fields.append(
        renderDataField("Banco/app", valueAt(receipt, "payment.bank_app")),
        renderDataField("Referencia", valueAt(receipt, "payment.reference")),
        renderDataField("Monto", `${valueAt(receipt, "payment.amount.value")} ${valueAt(receipt, "payment.amount.currency", "")}`.trim()),
        renderDataField("Fecha", valueAt(receipt, "payment.date_time.raw")),
        renderDataField("Concepto", valueAt(receipt, "payment.concept")),
        renderDataField("Estado", valueAt(receipt, "payment.status")),
        renderDataField("Telefono destino", valueAt(receipt, "recipient.phone")),
        renderDataField("Documento", valueAt(receipt, "recipient.document_id")),
        renderDataField("Banco destino", valueAt(receipt, "recipient.bank"))
      );

      const footer = document.createElement("div");
      footer.className = "receipt-footer";
      const key = document.createElement("div");
      key.innerHTML = `<strong>transaction_key:</strong> <span class="mono"></span>`;
      key.querySelector("span").textContent = valueAt(receipt, "transaction_key");
      footer.append(key);

      const missing = receipt.validation?.missing_fields || [];
      if (missing.length) {
        const missingNode = document.createElement("div");
        missingNode.innerHTML = "<strong>Campos faltantes:</strong> ";
        missingNode.append(document.createTextNode(missing.join(", ")));
        footer.append(missingNode);
      }

      const warnings = receipt.validation?.warnings || [];
      if (warnings.length) {
        const warningNode = document.createElement("div");
        warningNode.innerHTML = "<strong>Warnings:</strong> ";
        warningNode.append(document.createTextNode(warnings.join(", ")));
        footer.append(warningNode);
      }

      card.append(header, fields, footer);
      return card;
    })
  );
}

function renderErrors(errors) {
  if (!errors.length) {
    els.errorsList.className = "error-list empty-state";
    els.errorsList.textContent = "Sin errores";
    return;
  }

  els.errorsList.className = "error-list";
  els.errorsList.replaceChildren(
    ...errors.map((error) => {
      const card = document.createElement("article");
      card.className = "error-card";
      const title = document.createElement("h3");
      title.textContent = `${error.filename || "archivo"} - ${error.code || "error"}`;
      const message = document.createElement("p");
      message.textContent = error.message || "Error sin detalle";
      card.append(title, message);
      return card;
    })
  );
}

function renderResponse(response, durationMs) {
  state.lastResponse = response;
  const summary = response.summary || {};
  els.metricTotal.textContent = summary.total ?? 0;
  els.metricComplete.textContent = summary.complete ?? 0;
  els.metricIncomplete.textContent = summary.incomplete ?? 0;
  els.metricErrors.textContent = summary.errors ?? 0;
  els.metricDuration.textContent = `${(durationMs / 1000).toFixed(2)}s`;
  renderReceipts(response.receipts || []);
  renderErrors(response.errors || []);
  els.jsonOutput.textContent = JSON.stringify(response, null, 2);
  els.copyJsonButton.disabled = false;
  els.downloadJsonButton.disabled = false;
}

async function loadCapabilities() {
  try {
    const response = await fetch(`${API_PREFIX}/v1/capabilities`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.capabilities = await response.json();
    els.langInput.value = state.capabilities.options?.lang || "spa+eng";
    refreshLimitText();
    setServiceStatus("ok", "API lista");
    updateSubmitState();
  } catch (error) {
    refreshLimitText();
    setServiceStatus("error", "API no disponible");
    setFormMessage(error.message, true);
  }
}

async function submitFiles() {
  const { maxFiles } = limits();
  const files = selectedValidFiles();
  if (REQUIRE_API_KEY && !apiKeyValue()) {
    updateSubmitState();
    return;
  }
  if (!files.length || files.length > maxFiles) {
    updateSubmitState();
    return;
  }

  const form = new FormData();
  for (const file of files) {
    form.append("files", file, file.name);
  }
  form.append("lang", els.langInput.value.trim() || "spa+eng");
  form.append("include_raw_text", String(els.includeRawTextInput.checked));
  form.append("enable_crops", String(els.enableCropsInput.checked));

  state.busy = true;
  updateSubmitState();
  setFormMessage("Procesando");
  const started = performance.now();
  try {
    const response = await fetch(`${API_PREFIX}/v1/receipts/parse`, {
      method: "POST",
      body: form,
      headers: requestHeaders(),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || `HTTP ${response.status}`);
    }
    renderResponse(payload, performance.now() - started);
    setFormMessage(`request_id: ${payload.request_id || "-"}`);
  } catch (error) {
    setFormMessage(error.message || "Error procesando imagenes", true);
  } finally {
    state.busy = false;
    updateSubmitState();
  }
}

function switchTab(name) {
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `panel${name[0].toUpperCase()}${name.slice(1)}`);
  });
}

async function copyJson() {
  await navigator.clipboard.writeText(els.jsonOutput.textContent);
  setFormMessage("JSON copiado");
}

function downloadJson() {
  if (!state.lastResponse) return;
  const blob = new Blob([JSON.stringify(state.lastResponse, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const requestId = state.lastResponse.request_id || "response";
  link.href = url;
  link.download = `vepay-api-${requestId}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

els.fileInput.addEventListener("change", (event) => {
  addFiles(event.target.files);
  event.target.value = "";
});

els.dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  els.dropZone.classList.add("dragover");
});

els.dropZone.addEventListener("dragleave", () => {
  els.dropZone.classList.remove("dragover");
});

els.dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  els.dropZone.classList.remove("dragover");
  addFiles(event.dataTransfer.files);
});

els.clearFilesButton.addEventListener("click", () => {
  state.files = [];
  renderFiles();
});

els.submitButton.addEventListener("click", submitFiles);
els.copyJsonButton.addEventListener("click", copyJson);
els.downloadJsonButton.addEventListener("click", downloadJson);

document.querySelectorAll(".tab-button").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
});

if (els.apiKeyField) {
  els.apiKeyField.classList.toggle("hidden", !REQUIRE_API_KEY);
}

if (els.apiKeyInput) {
  els.apiKeyInput.addEventListener("input", updateSubmitState);
}

resetResults();
renderFiles();
loadCapabilities();
