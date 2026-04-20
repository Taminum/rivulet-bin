const forms = document.querySelectorAll("[data-paste-form]");
const THEME_KEY = "colorTheme";
const THEME_VALUES = ["dark", "light", "auto"];
const ACCOUNT_VIEW_KEY = "accountView";
const ACCOUNT_VIEW_VALUES = ["cards", "list"];
const historyDrawer = document.querySelector("[data-history-drawer]");
const accountShell = document.querySelector("[data-account-view-default]");
const tagsDialog = document.querySelector("[data-tags-dialog]");
const shareDialog = document.querySelector("[data-share-dialog]");
const SQL_START_RE = /^\s*(select|with|insert|update|delete|create|alter|drop)\b/i;
const HTML_START_RE = /^\s*<(?:!doctype|html|head|body|div|span|script|style|main|section|article|\w+-\w+)/i;
const CSS_RE = /(^|})\s*[^{}\n]+?\{\s*[^{}:;\n]+:\s*[^{};\n]+;\s*[^{}]*\}/s;
const YAML_RE = /^\s*[\w"'-]+\s*:\s*.+$/m;
const PYTHON_RE = /^\s*(def |class |from |import |async def |if __name__ == ['"]__main__['"]:)/m;
const BASH_RE = /^\s*(#!\/bin\/(ba)?sh|echo\b|export\b|if \[|for\b|while\b|case\b)/m;
const TYPESCRIPT_RE = /\b(interface|type|enum|implements|readonly|public|private|protected)\b|:\s*(string|number|boolean|unknown|never|void|Record<|Promise<|Array<)/;
const JAVASCRIPT_RE = /\b(const|let|var|function|console\.|document\.|window\.|import\s|export\s)\b|=>/;
const TEXT_WORD_RE = /[A-Za-zА-Яа-яЁё]{2,}/g;

function applyTheme(themePreference) {
  const nextPreference = THEME_VALUES.includes(themePreference) ? themePreference : "auto";
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const resolvedTheme = nextPreference === "auto" ? (prefersDark ? "dark" : "light") : nextPreference;

  document.documentElement.dataset.theme = resolvedTheme;
  document.documentElement.dataset.themePreference = nextPreference;

  for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    button.textContent = nextPreference[0].toUpperCase() + nextPreference.slice(1);
    button.setAttribute("title", `Theme: ${nextPreference}`);
  }
}

function triggerEditorLink(form, action) {
  const element = document.querySelector(`[data-editor-link="${action}"][data-editor-form="${form.id}"]`)
    || document.querySelector(`[data-editor-link="${action}"]`);
  if (!element) {
    return false;
  }

  if (element instanceof HTMLAnchorElement) {
    window.location.href = element.href;
    return true;
  }

  element.click();
  return true;
}

function execTextareaHistoryCommand(textarea, command) {
  if (!(textarea instanceof HTMLTextAreaElement) || typeof document.execCommand !== "function") {
    return false;
  }

  textarea.focus({ preventScroll: true });
  return document.execCommand(command);
}

function handleEditorShortcut(event, form, editor = null, textarea = null) {
  const primaryModifier = event.ctrlKey || event.metaKey;
  if (!primaryModifier && !event.altKey) {
    return false;
  }

  if (primaryModifier && !event.altKey && !event.shiftKey && event.code === "KeyS") {
    event.preventDefault();
    form.requestSubmit();
    return true;
  }

  if (primaryModifier && !event.altKey && !event.shiftKey && event.code === "KeyZ") {
    if (editor) {
      event.preventDefault();
      editor.getDoc().undo();
      return true;
    }

    return execTextareaHistoryCommand(textarea, "undo");
  }

  if (
    primaryModifier
    && !event.altKey
    && (
      (!event.shiftKey && event.code === "KeyY")
      || (event.shiftKey && event.code === "KeyZ")
    )
  ) {
    if (editor) {
      event.preventDefault();
      editor.getDoc().redo();
      return true;
    }

    return execTextareaHistoryCommand(textarea, "redo");
  }

  if (primaryModifier && event.shiftKey && !event.altKey && event.code === "KeyH") {
    event.preventDefault();
    setHistoryOpen(true);
    return true;
  }

  if (primaryModifier && event.shiftKey && !event.altKey && event.code === "KeyO") {
    event.preventDefault();
    return triggerEditorLink(form, "open");
  }

  if (primaryModifier && event.shiftKey && !event.altKey && event.code === "KeyL") {
    event.preventDefault();
    return triggerEditorLink(form, "changes");
  }

  if (!primaryModifier && event.altKey && !event.shiftKey && event.code === "KeyQ" && editor) {
    event.preventDefault();
    editor.foldCode(editor.getCursor());
    return true;
  }

  return false;
}

function setHistoryOpen(isOpen) {
  if (!historyDrawer) {
    return;
  }

  historyDrawer.setAttribute("data-open", isOpen ? "true" : "false");
  historyDrawer.setAttribute("aria-hidden", isOpen ? "false" : "true");
  document.body.classList.toggle("history-open", isOpen);
}

function setShareDialogOpen(isOpen) {
  if (!shareDialog) {
    return;
  }

  shareDialog.setAttribute("data-open", isOpen ? "true" : "false");
  shareDialog.setAttribute("aria-hidden", isOpen ? "false" : "true");
  document.body.classList.toggle("share-open", isOpen);
}

function setTagsDialogOpen(isOpen) {
  if (!tagsDialog) {
    return;
  }

  tagsDialog.setAttribute("data-open", isOpen ? "true" : "false");
  tagsDialog.setAttribute("aria-hidden", isOpen ? "false" : "true");
  document.body.classList.toggle("tags-open", isOpen);

  if (isOpen) {
    const input = tagsDialog.querySelector("[data-tags-input]");
    if (input instanceof HTMLInputElement) {
      window.requestAnimationFrame(() => {
        input.focus();
        input.select();
      });
    }
  }
}

function populateShareDialog(button) {
  if (!shareDialog || !button) {
    return;
  }

  const title = button.getAttribute("data-share-title") || "Paste links";
  const slug = button.getAttribute("data-share-slug") || "";
  const publicUrl = button.getAttribute("data-share-public-url") || "";
  const rawUrl = button.getAttribute("data-share-raw-url") || "";
  const editUrl = button.getAttribute("data-share-edit-url") || "";
  const hasRaw = button.getAttribute("data-share-has-raw") === "true";

  const titleNode = shareDialog.querySelector("[data-share-dialog-title]");
  const slugNode = shareDialog.querySelector("[data-share-dialog-slug]");
  if (titleNode) {
    titleNode.textContent = title;
  }
  if (slugNode) {
    slugNode.textContent = slug;
  }

  for (const [field, value] of Object.entries({ public: publicUrl, raw: rawUrl, edit: editUrl })) {
    const input = shareDialog.querySelector(`[data-share-input="${field}"]`);
    const copyButton = shareDialog.querySelector(`[data-share-copy="${field}"]`);
    if (input) {
      input.value = value;
    }
    if (copyButton) {
      copyButton.setAttribute("data-copy", value);
      copyButton.textContent = "Copy";
    }
  }

  const rawRow = shareDialog.querySelector('[data-share-row="raw"]');
  if (rawRow) {
    rawRow.hidden = !hasRaw;
  }
}

function populateTagsDialog(button) {
  if (!tagsDialog || !button) {
    return;
  }

  const slug = button.getAttribute("data-tags-slug") || "";
  const title = button.getAttribute("data-tags-title") || slug || "Edit tags";
  const value = (button.getAttribute("data-tags-value") || "").trim();
  const tab = button.getAttribute("data-tags-tab") || "saved";
  const filterTag = button.getAttribute("data-tags-filter") || "";

  const titleNode = tagsDialog.querySelector("[data-tags-dialog-title]");
  const slugNode = tagsDialog.querySelector("[data-tags-dialog-slug]");
  const form = tagsDialog.querySelector("[data-tags-form]");
  const input = tagsDialog.querySelector("[data-tags-input]");
  const tabInput = tagsDialog.querySelector("[data-tags-form-tab]");
  const filterInput = tagsDialog.querySelector("[data-tags-form-filter]");

  if (titleNode) {
    titleNode.textContent = title;
  }
  if (slugNode) {
    slugNode.textContent = slug;
  }
  if (form instanceof HTMLFormElement) {
    form.action = `/account/tags/${slug}`;
  }
  if (input instanceof HTMLInputElement) {
    input.value = value;
  }
  if (tabInput instanceof HTMLInputElement) {
    tabInput.value = tab;
  }
  if (filterInput instanceof HTMLInputElement) {
    filterInput.value = filterTag;
  }
}

function setAccountView(viewPreference) {
  if (!accountShell) {
    return;
  }

  const defaultView = accountShell.dataset.accountViewDefault || "cards";
  const nextView = ACCOUNT_VIEW_VALUES.includes(viewPreference) ? viewPreference : defaultView;
  accountShell.dataset.accountView = nextView;

  for (const button of document.querySelectorAll("[data-account-view-toggle]")) {
    const isActive = button.dataset.view === nextView;
    button.dataset.active = isActive ? "true" : "false";
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
  }
}

function getAssociatedField(form, name) {
  return document.querySelector(`[form="${form.id}"][name="${name}"]`) || form.querySelector(`[name="${name}"]`);
}

function looksLikeJson(content) {
  return Boolean(content) && /^[\[{]/.test(content);
}

function looksLikeYaml(content) {
  if (content.startsWith("---")) {
    return true;
  }

  if (content.startsWith("{") || content.startsWith("[")) {
    return false;
  }

  return YAML_RE.test(content);
}

function looksLikeCss(content) {
  if (content.startsWith("{") || content.startsWith("[")) {
    return false;
  }

  if (content.includes("<") || content.includes("function")) {
    return false;
  }

  return CSS_RE.test(content);
}

function countPatternMatches(content, pattern) {
  const matches = content.match(pattern);
  return matches ? matches.length : 0;
}

function looksLikePlainText(content) {
  if (content.length < 120) {
    return false;
  }

  const lines = content
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  const wordCount = countPatternMatches(content, TEXT_WORD_RE);
  if (wordCount < 18) {
    return false;
  }

  const codeSymbolCount = Array.from(content).filter((symbol) => "{}[]();<>`$\\".includes(symbol)).length;
  if (codeSymbolCount > Math.max(6, Math.floor(content.length / 100))) {
    return false;
  }

  const sentencePunctuationCount = Array.from(content).filter((symbol) => ".!?".includes(symbol)).length;
  const longTextLineCount = lines.filter((line) => countPatternMatches(line, TEXT_WORD_RE) >= 5).length;
  const hasTextShape = lines.length >= 3 || sentencePunctuationCount >= 2;
  return hasTextShape && longTextLineCount >= 1;
}

function isSyntaxAutoManaged(syntaxField) {
  return syntaxField.value === "auto" || syntaxField.dataset.syntaxManagement === "auto";
}

function detectSyntaxFromText(content) {
  const trimmed = content.trim();
  if (!trimmed) {
    return "auto";
  }

  if (looksLikeJson(trimmed)) {
    return "json";
  }

  if (looksLikeYaml(trimmed)) {
    return "yaml";
  }

  if (HTML_START_RE.test(trimmed)) {
    return "html";
  }

  if (SQL_START_RE.test(trimmed)) {
    return "sql";
  }

  if (BASH_RE.test(trimmed)) {
    return "bash";
  }

  if (PYTHON_RE.test(trimmed)) {
    return "python";
  }

  if (TYPESCRIPT_RE.test(trimmed)) {
    return "typescript";
  }

  if (JAVASCRIPT_RE.test(trimmed)) {
    return "javascript";
  }

  if (looksLikeCss(trimmed)) {
    return "css";
  }

  if (looksLikePlainText(trimmed)) {
    return "text";
  }

  return "auto";
}

function registerCodeMirrorModes() {
  if (typeof window.CodeMirror === "undefined" || window.CodeMirror._rivuletModesRegistered) {
    return;
  }

  const cm = window.CodeMirror;
  const jsonPropertyPattern = /^"(?:[^\\"]|\\.)*"\s*(?=:)/;
  const yamlPropertyPattern = /^(?:[A-Za-z0-9_"'-][^:\n]*?)(?=\s*:)/;

  cm.defineMode("rivulet-json", (config) =>
    cm.overlayMode(cm.getMode(config, { name: "javascript", json: true }), {
      token(stream) {
        if (stream.match(jsonPropertyPattern)) {
          return "property";
        }

        stream.next();
        return null;
      },
    }));

  cm.defineMode("rivulet-yaml", (config) =>
    cm.overlayMode(cm.getMode(config, "yaml"), {
      token(stream) {
        if (stream.sol()) {
          stream.eatSpace();
          stream.eatWhile(/-/);
          stream.eatSpace();
        }

        if (stream.match(yamlPropertyPattern)) {
          return "property";
        }

        stream.next();
        return null;
      },
    }));

  window.CodeMirror._rivuletModesRegistered = true;
}

function getEditorMode(syntax, mode) {
  if (mode === "link") {
    return null;
  }

  if (mode === "markdown" || syntax === "markdown" || syntax === "text" || syntax === "auto") {
    return null;
  }

  switch (syntax) {
    case "json":
      return "rivulet-json";
    case "javascript":
      return "javascript";
    case "typescript":
      return "text/typescript";
    case "yaml":
      return "rivulet-yaml";
    case "python":
      return "python";
    case "sql":
      return "text/x-sql";
    case "html":
      return "xml";
    case "css":
      return "css";
    case "bash":
      return "shell";
    default:
      return null;
  }
}

function shouldWrapEditor(mode, syntax) {
  return mode === "markdown" || syntax === "markdown";
}

function updateEditorLanguage(editor, syntaxField, modeField) {
  if (!editor) {
    return;
  }

  const syntax = syntaxField?.value || "auto";
  const mode = modeField?.value || "auto";
  editor.setOption("mode", getEditorMode(syntax, mode));
  editor.setOption("lineWrapping", shouldWrapEditor(mode, syntax));
}

function getCharacterOffset(text, line, column) {
  if (!line || !column) {
    return 0;
  }

  const lines = text.split("\n");
  let offset = 0;
  for (let index = 0; index < line - 1 && index < lines.length; index += 1) {
    offset += lines[index].length + 1;
  }
  return Math.min(offset + Math.max(column - 1, 0), text.length);
}

function updateValidationMarker(wrapper, textarea) {
  const marker = wrapper.querySelector(".validation-line-marker");
  const line = Number(wrapper.dataset.validationLine || "");
  if (!marker || !line) {
    return;
  }

  const style = window.getComputedStyle(textarea);
  const lineHeight = Number.parseFloat(style.lineHeight) || 24;
  const paddingTop = Number.parseFloat(style.paddingTop) || 0;
  const top = paddingTop + (line - 1) * lineHeight - textarea.scrollTop;
  marker.style.top = `${top}px`;
  marker.style.height = `${lineHeight}px`;

  const isVisible = top + lineHeight >= 0 && top <= textarea.clientHeight;
  marker.style.display = isVisible ? "block" : "none";
}

function clearValidationState(wrapper, editor = null) {
  if (!wrapper) {
    return;
  }

  wrapper.classList.remove("has-validation-error");
  delete wrapper.dataset.validationLine;
  delete wrapper.dataset.validationColumn;

  const marker = wrapper.querySelector(".validation-line-marker");
  if (marker) {
    marker.style.display = "none";
  }

  if (!editor || !editor._rivuletValidationLineHandle) {
    return;
  }

  editor.removeLineClass(editor._rivuletValidationLineHandle, "background", "cm-validation-line");
  editor.removeLineClass(editor._rivuletValidationLineHandle, "wrap", "cm-validation-line-wrap");
  editor._rivuletValidationLineHandle = null;
}

function applyTextareaValidationHighlight(wrapper, textarea) {
  const line = Number(wrapper.dataset.validationLine || "");
  const column = Number(wrapper.dataset.validationColumn || "");
  if (!line) {
    return;
  }

  const style = window.getComputedStyle(textarea);
  const lineHeight = Number.parseFloat(style.lineHeight) || 24;
  const paddingTop = Number.parseFloat(style.paddingTop) || 0;
  textarea.scrollTop = Math.max(0, paddingTop + Math.max(line - 2, 0) * lineHeight - lineHeight);

  const offset = getCharacterOffset(textarea.value, line, column || 1);
  textarea.focus({ preventScroll: true });
  textarea.setSelectionRange(offset, Math.min(offset + 1, textarea.value.length));

  updateValidationMarker(wrapper, textarea);
}

function applyEditorValidationHighlight(wrapper, editor) {
  const line = Number(wrapper.dataset.validationLine || "");
  const column = Number(wrapper.dataset.validationColumn || "");
  if (!line) {
    return;
  }

  clearValidationState(wrapper, editor);
  wrapper.classList.add("has-validation-error");
  wrapper.dataset.validationLine = String(line);
  wrapper.dataset.validationColumn = String(column || "");

  const safeLine = Math.min(Math.max(line - 1, 0), Math.max(editor.lineCount() - 1, 0));
  const lineHandle = editor.getLineHandle(safeLine);
  if (!lineHandle) {
    return;
  }

  editor._rivuletValidationLineHandle = lineHandle;
  editor.addLineClass(lineHandle, "background", "cm-validation-line");
  editor.addLineClass(lineHandle, "wrap", "cm-validation-line-wrap");

  const lineText = editor.getLine(safeLine) || "";
  const from = { line: safeLine, ch: Math.min(Math.max((column || 1) - 1, 0), lineText.length) };
  const to = { line: safeLine, ch: Math.min(from.ch + 1, lineText.length) };

  window.requestAnimationFrame(() => {
    editor.focus();
    if (to.ch > from.ch) {
      editor.setSelection(from, to);
    } else {
      editor.setCursor(from);
    }
    editor.scrollIntoView({ from, to }, 120);
  });
}

function syncAutoDetectedSyntax(getValue, syntaxField, modeField, editor = null) {
  if (!syntaxField || !isSyntaxAutoManaged(syntaxField)) {
    return;
  }

  if (modeField && ["markdown", "link"].includes(modeField.value)) {
    return;
  }

  syntaxField.value = detectSyntaxFromText(getValue());
  syntaxField.dataset.syntaxManagement = "auto";
  updateEditorLanguage(editor, syntaxField, modeField);
}

function buildCodeEditor(form, textarea, syntaxField, modeField, wrapper) {
  if (typeof window.CodeMirror === "undefined") {
    return null;
  }

  const editor = window.CodeMirror.fromTextArea(textarea, {
    theme: "rivulet",
    mode: getEditorMode(syntaxField?.value || "auto", modeField?.value || "auto"),
    lineNumbers: true,
    lineWrapping: shouldWrapEditor(modeField?.value || "auto", syntaxField?.value || "auto"),
    styleActiveLine: true,
    matchBrackets: true,
    indentUnit: 2,
    tabSize: 2,
    indentWithTabs: false,
    smartIndent: true,
    foldGutter: true,
    gutters: ["CodeMirror-linenumbers", "CodeMirror-foldgutter"],
    extraKeys: {
      Tab(cm) {
        if (cm.somethingSelected()) {
          cm.indentSelection("add");
          return;
        }
        cm.replaceSelection("  ", "end", "+input");
      },
      "Shift-Tab"(cm) {
        cm.indentSelection("subtract");
      },
      "Ctrl-Z"(cm) {
        cm.getDoc().undo();
      },
      "Cmd-Z"(cm) {
        cm.getDoc().undo();
      },
      "Shift-Ctrl-Z"(cm) {
        cm.getDoc().redo();
      },
      "Shift-Cmd-Z"(cm) {
        cm.getDoc().redo();
      },
      "Ctrl-Y"(cm) {
        cm.getDoc().redo();
      },
      "Cmd-Y"(cm) {
        cm.getDoc().redo();
      },
      "Ctrl-S"() {
        form.requestSubmit();
      },
      "Cmd-S"() {
        form.requestSubmit();
      },
      "Ctrl-Q"(cm) {
        cm.foldCode(cm.getCursor());
      },
    },
  });

  wrapper?.classList.add("is-editor-ready");
  updateEditorLanguage(editor, syntaxField, modeField);
  return editor;
}

function initializeCodeEditor(form, textarea, syntaxField, modeField, wrapper) {
  const editor = buildCodeEditor(form, textarea, syntaxField, modeField, wrapper);
  if (!editor) {
    return null;
  }

  let syntaxSyncTimer = null;
  const scheduleSyntaxSync = () => {
    if (!syntaxField || !isSyntaxAutoManaged(syntaxField)) {
      return;
    }

    window.clearTimeout(syntaxSyncTimer);
    syntaxSyncTimer = window.setTimeout(() => {
      syncAutoDetectedSyntax(() => editor.getValue(), syntaxField, modeField, editor);
    }, 80);
  };

  editor.on("change", (_instance, change) => {
    clearValidationState(wrapper, editor);

    if (change.origin === "paste") {
      scheduleSyntaxSync();
    }
  });

  editor.on("keydown", (_instance, event) => {
    handleEditorShortcut(event, form, editor, textarea);
  });

  form.addEventListener("submit", () => {
    editor.save();
  });

  if (syntaxField) {
    syntaxField.addEventListener("change", () => {
      syntaxField.dataset.syntaxManagement = syntaxField.value === "auto" ? "auto" : "manual";

      if (syntaxField.value === "auto") {
        scheduleSyntaxSync();
        return;
      }

      updateEditorLanguage(editor, syntaxField, modeField);
    });
  }

  if (modeField) {
    modeField.addEventListener("change", () => {
      updateEditorLanguage(editor, syntaxField, modeField);

      if (syntaxField && isSyntaxAutoManaged(syntaxField)) {
        scheduleSyntaxSync();
      }
    });
  }

  editor.getWrapperElement().addEventListener("paste", () => {
    scheduleSyntaxSync();
  });

  if (wrapper?.dataset.validationLine) {
    applyEditorValidationHighlight(wrapper, editor);
  }

  if (syntaxField && isSyntaxAutoManaged(syntaxField) && editor.getValue().trim()) {
    scheduleSyntaxSync();
  }

  return editor;
}

function initializePlainTextarea(form, textarea, syntaxField, modeField, wrapper) {
  if (wrapper?.dataset.validationLine) {
    applyTextareaValidationHighlight(wrapper, textarea);
    textarea.addEventListener("scroll", () => updateValidationMarker(wrapper, textarea));
  }

  textarea.addEventListener("input", () => {
    clearValidationState(wrapper);
  });

  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const value = textarea.value;
      textarea.value = `${value.slice(0, start)}  ${value.slice(end)}`;
      textarea.selectionStart = textarea.selectionEnd = start + 2;
    }

    if (handleEditorShortcut(event, form, null, textarea)) {
      return;
    }
  });

  if (syntaxField) {
    syntaxField.addEventListener("change", () => {
      syntaxField.dataset.syntaxManagement = syntaxField.value === "auto" ? "auto" : "manual";
      if (syntaxField.value === "auto") {
        syncAutoDetectedSyntax(() => textarea.value, syntaxField, modeField);
      }
    });
  }

  if (modeField) {
    modeField.addEventListener("change", () => {
      if (syntaxField && isSyntaxAutoManaged(syntaxField)) {
        syncAutoDetectedSyntax(() => textarea.value, syntaxField, modeField);
      }
    });
  }

  textarea.addEventListener("paste", () => {
    window.setTimeout(() => {
      syncAutoDetectedSyntax(() => textarea.value, syntaxField, modeField);
    }, 0);
  });
}

applyTheme(localStorage.getItem(THEME_KEY) || "dark");
setHistoryOpen(false);
setAccountView(localStorage.getItem(ACCOUNT_VIEW_KEY) || accountShell?.dataset.accountViewDefault || "cards");

const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
if (typeof mediaQuery.addEventListener === "function") {
  mediaQuery.addEventListener("change", () => {
    if ((localStorage.getItem(THEME_KEY) || "auto") === "auto") {
      applyTheme("auto");
    }
  });
}

for (const button of document.querySelectorAll("[data-theme-toggle]")) {
  button.addEventListener("click", () => {
    const current = localStorage.getItem(THEME_KEY) || "dark";
    const nextIndex = (THEME_VALUES.indexOf(current) + 1) % THEME_VALUES.length;
    const nextTheme = THEME_VALUES[nextIndex];
    localStorage.setItem(THEME_KEY, nextTheme);
    applyTheme(nextTheme);
  });
}

for (const button of document.querySelectorAll("[data-account-view-toggle]")) {
  button.addEventListener("click", () => {
    const nextView = button.dataset.view || "cards";
    localStorage.setItem(ACCOUNT_VIEW_KEY, nextView);
    setAccountView(nextView);
  });
}

for (const button of document.querySelectorAll("[data-history-open]")) {
  button.addEventListener("click", () => {
    setHistoryOpen(true);
  });
}

for (const button of document.querySelectorAll("[data-history-close]")) {
  button.addEventListener("click", () => {
    setHistoryOpen(false);
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setHistoryOpen(false);
    setTagsDialogOpen(false);
    setShareDialogOpen(false);
  }
});

registerCodeMirrorModes();

for (const form of forms) {
  const textarea = form.querySelector(".editor-source");
  if (!textarea) {
    continue;
  }

  const syntaxField = getAssociatedField(form, "syntax");
  const modeField = getAssociatedField(form, "mode");
  const wrapper = textarea.closest(".editor-field");

  if (syntaxField && !syntaxField.dataset.syntaxManagement) {
    syntaxField.dataset.syntaxManagement = syntaxField.value === "auto" ? "auto" : "manual";
  }

  const editor = initializeCodeEditor(form, textarea, syntaxField, modeField, wrapper);
  if (!editor) {
    initializePlainTextarea(form, textarea, syntaxField, modeField, wrapper);
  }
}

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const value = button.getAttribute("data-copy");
    if (!value || !navigator.clipboard) {
      return;
    }

    await navigator.clipboard.writeText(value);
    const original = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => {
      button.textContent = original;
    }, 1400);
  });
}

for (const form of document.querySelectorAll("[data-delete-form]")) {
  form.addEventListener("submit", (event) => {
    const label = form.getAttribute("data-delete-label") || "this paste";
    const confirmed = window.confirm(`Delete "${label}"? This action cannot be undone.`);
    if (!confirmed) {
      event.preventDefault();
    }
  });
}

for (const button of document.querySelectorAll("[data-share-open]")) {
  button.addEventListener("click", () => {
    populateShareDialog(button);
    setShareDialogOpen(true);
  });
}

for (const button of document.querySelectorAll("[data-tags-open]")) {
  button.addEventListener("click", () => {
    populateTagsDialog(button);
    setTagsDialogOpen(true);
  });
}

for (const button of document.querySelectorAll("[data-tags-close]")) {
  button.addEventListener("click", () => {
    setTagsDialogOpen(false);
  });
}

for (const button of document.querySelectorAll("[data-share-close]")) {
  button.addEventListener("click", () => {
    setShareDialogOpen(false);
  });
}
