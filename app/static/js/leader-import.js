(() => {
  const form = document.getElementById("leader-import-form");
  const panel = document.getElementById("import-preview");
  if (!form || !panel) return;

  const rowsBody = document.getElementById("import-preview-rows");
  const summary = document.getElementById("import-preview-summary");
  const message = document.getElementById("import-preview-message");
  const confirmButton = document.getElementById("confirm-import");
  const reportButton = document.getElementById("download-import-report");
  const credentialsPanel = document.getElementById("import-credentials");
  let previewReady = false;
  let completed = false;
  let previewRows = [];

  const issueText = (row) => [...(row.errors || []), ...(row.warnings || [])].join("; ");

  function showRows(rows) {
    rowsBody.replaceChildren();
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const value of [
        row.row_number,
        row.full_name,
        (row.status || "").replaceAll("_", " "),
        issueText(row),
      ]) {
        const td = document.createElement("td");
        td.textContent = String(value ?? "");
        tr.append(td);
      }
      rowsBody.append(tr);
    }
  }

  function showCredentials(credentials) {
    if (!credentials || credentials.length === 0) {
      credentialsPanel.hidden = true;
      return;
    }
    credentialsPanel.replaceChildren();
    const heading = document.createElement("h2");
    heading.textContent = "One-time participant credentials";
    const note = document.createElement("p");
    note.textContent = "Save and securely deliver these details now. The PINs will not be available again after you leave this page.";
    const wrapper = document.createElement("div");
    wrapper.className = "table-wrap";
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const label of ["Name", "Reference", "PIN"]) {
      const th = document.createElement("th");
      th.textContent = label;
      headerRow.append(th);
    }
    thead.append(headerRow);
    const tbody = document.createElement("tbody");
    for (const item of credentials) {
      const tr = document.createElement("tr");
      for (const value of [item.name, item.reference, item.pin]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    wrapper.append(table);
    credentialsPanel.append(heading, note, wrapper);
    credentialsPanel.hidden = false;
  }

  function showPreview(data) {
    previewReady = true;
    panel.hidden = false;
    message.className = "notice notice-info";
    previewRows = data.rows || [];
    showRows(previewRows);
    summary.textContent = `${data.eligible_count} eligible · ${data.error_count} row error(s) · ${data.duplicate_count} duplicate warning(s)`;
    message.textContent = data.message || "Review the rows. Errors and duplicates will be skipped.";
    confirmButton.disabled = completed || data.eligible_count === 0;
    reportButton.hidden = data.error_count === 0 && data.duplicate_count === 0;
    showCredentials(data.credentials);
  }

  async function postForm(url) {
    const response = await fetch(url, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The import request failed.");
    return data;
  }

  function showError(error) {
    panel.hidden = false;
    message.className = "notice notice-danger";
    message.textContent = error.message || "The import request failed.";
  }

  function addSubmissions(credentials) {
    const body = document.getElementById("leader-submissions");
    if (!body || credentials.length === 0) return;
    const emptyRow = body.querySelector("[data-empty-submissions]");
    if (emptyRow) emptyRow.remove();
    const eventName = form.querySelector("#import_event option:checked")?.textContent || "";
    const date = new Date().toISOString().slice(0, 10);
    for (const item of credentials) {
      const tr = document.createElement("tr");
      for (const value of [item.name, eventName, "pending", date]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      body.prepend(tr);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    panel.hidden = false;
    message.className = "notice notice-info";
    message.textContent = "Checking the file…";
    try {
      showPreview(await postForm(form.action));
    } catch (error) {
      previewReady = false;
      confirmButton.disabled = true;
      showError(error);
    }
  });

  confirmButton.addEventListener("click", async () => {
    if (!previewReady || completed) return;
    confirmButton.disabled = true;
    message.textContent = "Rechecking and submitting eligible rows…";
    try {
      const data = await postForm(form.action.replace(/\/preview$/, "/confirm"));
      completed = true;
      showPreview(data);
      addSubmissions(data.credentials || []);
    } catch (error) {
      showError(error);
      confirmButton.disabled = false;
    }
  });

  reportButton.addEventListener("click", async () => {
    reportButton.disabled = true;
    try {
      const reportRows = previewRows.flatMap((row) =>
        [
          ...(row.errors || []).map((detail) => ["error", detail]),
          ...(row.warnings || []).map((detail) => ["duplicate_warning", detail]),
        ].map(([kind, detail]) => [
          row.row_number,
          safeCsvValue(row.full_name),
          kind,
          detail,
        ]),
      );
      const report = [
        ["row_number", "full_name", "issue_type", "details"],
        ...reportRows,
      ]
        .map((row) => row.map(csvCell).join(","))
        .join("\r\n") + "\r\n";
      const auditData = new FormData();
      auditData.set("csrf_token", form.querySelector('[name="csrf_token"]').value);
      auditData.set("event_id", form.querySelector('[name="event_id"]').value);
      auditData.set("issue_count", String(reportRows.length));
      const auditResponse = await fetch(
        form.action.replace(/\/preview$/, "/report-audit"),
        {
          method: "POST",
          body: auditData,
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest" },
        },
      );
      if (!auditResponse.ok) {
        const data = await auditResponse.json();
        throw new Error(data.error || "The error report could not be created.");
      }
      const url = URL.createObjectURL(
        new Blob(["\ufeff", report], { type: "text/csv;charset=utf-8" }),
      );
      const download = document.createElement("a");
      download.href = url;
      download.download = "bunotan-import-errors.csv";
      download.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      showError(error);
    } finally {
      reportButton.disabled = false;
    }
  });

  form.addEventListener("change", () => {
    previewReady = false;
    completed = false;
    previewRows = [];
    panel.hidden = true;
    confirmButton.disabled = true;
  });

  function safeCsvValue(value) {
    const text = String(value || "");
    return /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  }

  function csvCell(value) {
    return `"${String(value ?? "").replaceAll('"', '""')}"`;
  }
})();
