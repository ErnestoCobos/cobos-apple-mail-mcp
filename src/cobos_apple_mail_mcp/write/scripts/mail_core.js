// mail_core.js — JXA helper library for cobos-apple-mail-mcp's write layer.
//
// Loaded once per osascript invocation alongside a single function call
// (see write/jxa_executor.py::run_jxa). Every operation here is a real
// Apple Event / JXA method call against Application("Mail") — never
// simulated keystrokes or NSPasteboard injection (CLAUDE.md invariant #5).
//
// CORRECTNESS NOTE: verified against a live Mail.app. Real testing corrected
// several assumptions the documented dictionary alone got wrong:
//   - `messages whose message id is X` needs the id WITHOUT angle brackets
//     (see matchByMessageId).
//   - `ToRecipient`/`CcRecipient`/`BccRecipient`/`Attachment` and rule
//     conditions CANNOT be `.make()`'d standalone (error -10024); construct
//     and push them directly (addRecipients/addAttachments).
//   - An OutgoingMessage's subject/content must be set by ASSIGNMENT, not via
//     make({withProperties}) (which silently drops them, then Mail blocks the
//     send on a "no subject" dialog).
//   - Compose sends from the message's `sender` address; set it from the
//     requested account or the send goes out from Mail's default account.
// Known residual quirk: sent OutgoingMessages linger in app.outgoingMessages()
// and app.delete() won't clear them — the send/delivery still succeeds; the
// phantoms clear on a Mail relaunch.

function MailApp() {
  return Application("Mail");
}

function ping(args) {
  var app = MailApp();
  return { ok: true, running: app.running() };
}

function accountEmailAddresses(account) {
  try {
    return account.emailAddresses() || [];
  } catch (e) {
    return [];
  }
}

function findAccount(app, hint) {
  var accounts = app.accounts();
  if (!hint) {
    return accounts.length ? accounts[0] : null;
  }
  var lowerHint = String(hint).toLowerCase();
  for (var i = 0; i < accounts.length; i++) {
    if (accounts[i].name().toLowerCase() === lowerHint) return accounts[i];
  }
  for (var i = 0; i < accounts.length; i++) {
    var addrs = accountEmailAddresses(accounts[i]);
    for (var j = 0; j < addrs.length; j++) {
      if (String(addrs[j]).toLowerCase() === lowerHint) return accounts[i];
    }
  }
  return null;
}

var MAILBOX_ALIASES = {
  sent: ["sent messages", "sent items", "sent"],
  trash: ["deleted messages", "trash", "bin"],
  drafts: ["drafts"],
  junk: ["junk", "junk e-mail", "spam"],
  inbox: ["inbox"]
};

function findMailbox(account, hint) {
  if (!account) return null;
  var mailboxes = account.mailboxes();
  if (!hint) {
    for (var i = 0; i < mailboxes.length; i++) {
      if (mailboxes[i].name().toLowerCase() === "inbox") return mailboxes[i];
    }
    return mailboxes.length ? mailboxes[0] : null;
  }
  var lowerHint = String(hint).toLowerCase();
  for (var i = 0; i < mailboxes.length; i++) {
    if (mailboxes[i].name().toLowerCase() === lowerHint) return mailboxes[i];
  }
  var aliases = MAILBOX_ALIASES[lowerHint];
  if (aliases) {
    for (var i = 0; i < mailboxes.length; i++) {
      var name = mailboxes[i].name().toLowerCase();
      if (aliases.indexOf(name) !== -1) return mailboxes[i];
    }
  }
  return null;
}

// Like findMailbox but also descends into nested mailboxes (Mail lets a
// mailbox contain sub-mailboxes; account.mailboxes() only returns the top
// level). Used for a move TARGET so a subfolder destination resolves too.
function _searchNestedMailbox(mailbox, lowerHint) {
  try {
    if (mailbox.name().toLowerCase() === lowerHint) return mailbox;
    var children = mailbox.mailboxes();
    for (var i = 0; i < children.length; i++) {
      var found = _searchNestedMailbox(children[i], lowerHint);
      if (found) return found;
    }
  } catch (e) {}
  return null;
}

function findMailboxDeep(account, hint) {
  var flat = findMailbox(account, hint);
  if (flat) return flat;
  var lowerHint = String(hint == null ? "" : hint).toLowerCase();
  if (!lowerHint) return null;
  var tops = account.mailboxes();
  for (var i = 0; i < tops.length; i++) {
    var found = _searchNestedMailbox(tops[i], lowerHint);
    if (found) return found;
  }
  return null;
}

function listAccounts(args) {
  var app = MailApp();
  var accounts = app.accounts();
  var result = [];
  for (var i = 0; i < accounts.length; i++) {
    result.push({
      name: accounts[i].name(),
      emailAddresses: accountEmailAddresses(accounts[i])
    });
  }
  return result;
}

function listMailboxes(args) {
  var app = MailApp();
  var account = findAccount(app, args.account);
  if (!account) return [];
  var mailboxes = account.mailboxes();
  var result = [];
  for (var i = 0; i < mailboxes.length; i++) {
    result.push({ name: mailboxes[i].name(), unreadCount: mailboxes[i].unreadCount() });
  }
  return result;
}

function describeMessage(msg, accountName, mailboxName) {
  var dateSent = null;
  try {
    dateSent = msg.dateSent() ? msg.dateSent().toISOString() : null;
  } catch (e) {}
  return {
    account: accountName,
    mailbox: mailboxName,
    mailInternalId: msg.id(),
    messageId: msg.messageId(),
    subject: msg.subject(),
    dateSent: dateSent
  };
}

// The core resolution primitive: scoped search for a message by its RFC822
// Message-ID (args.messageId, bracketed). Returns every candidate found in
// scope so core/resolver.py can read-back-verify and detect ambiguity —
// this function never picks a "best" match itself.
// Match messages by RFC822 Message-ID. Verified against real Mail: JXA's
// message.messageId() returns the id WITHOUT angle brackets, so a
// whose({messageId:"<...>"}) query matches nothing. We query the
// bracket-stripped form first (what real Mail stores) and fall back to the
// original for robustness across Mail versions. Getting this wrong makes
// every scoped resolve return zero and fall through to the unbounded broad
// scan, which times out on large mailboxes — a real bug this fixes.
function matchByMessageId(collection, rawId) {
  var stripped = String(rawId).replace(/^<+/, "").replace(/>+$/, "");
  var forms = stripped === rawId ? [stripped] : [stripped, rawId];
  for (var i = 0; i < forms.length; i++) {
    try {
      var hits = collection.whose({ messageId: forms[i] })();
      if (hits.length > 0) return hits;
    } catch (e) {
      /* try next form */
    }
  }
  return [];
}

function resolveMessage(args) {
  var app = MailApp();
  var targetId = args.messageId;
  var candidates = [];

  var accountsToSearch = [];
  if (args.accountHint) {
    var acc = findAccount(app, args.accountHint);
    if (acc) accountsToSearch.push(acc);
  }
  if (accountsToSearch.length === 0) accountsToSearch = app.accounts();

  for (var a = 0; a < accountsToSearch.length; a++) {
    var account = accountsToSearch[a];
    var mailboxesToSearch = [];
    if (args.mailboxHint) {
      var mb = findMailbox(account, args.mailboxHint);
      if (mb) mailboxesToSearch.push(mb);
    }
    if (mailboxesToSearch.length === 0) mailboxesToSearch = account.mailboxes();

    for (var m = 0; m < mailboxesToSearch.length; m++) {
      var mailbox = mailboxesToSearch[m];
      var hits = matchByMessageId(mailbox.messages, targetId);
      for (var h = 0; h < hits.length; h++) {
        candidates.push(describeMessage(hits[h], account.name(), mailbox.name()));
      }
    }
  }
  return { candidates: candidates };
}

// Re-resolve to exactly one live message OBJECT (not just its description),
// given an already-known account/mailbox name (from a prior resolveMessage
// call) — avoids repeating a broad scan for the actual mutation.
function getMessageHandle(args) {
  var app = MailApp();
  var account = findAccount(app, args.accountHint);
  if (!account) throw "account not found: " + args.accountHint;
  var mailbox = findMailbox(account, args.mailboxHint);
  if (!mailbox) throw "mailbox not found: " + args.mailboxHint;
  var hits = matchByMessageId(mailbox.messages, args.messageId);
  if (hits.length === 0) throw "message not found on re-resolution (it may have moved)";
  return { app: app, account: account, mailbox: mailbox, message: hits[0] };
}

function moveEmail(args) {
  var handle = getMessageHandle(args);
  // The destination may live in a DIFFERENT account than the message. Resolve
  // the target mailbox in the named target account (default: the message's own
  // account), searching nested mailboxes too — Mail's move() happily moves
  // across accounts once we hand it the right mailbox object. Without this the
  // target was only ever looked up in the source account (JXA error -2700).
  var targetAccount = handle.account;
  if (args.toAccount) {
    targetAccount = findAccount(handle.app, args.toAccount);
    if (!targetAccount) throw "target account not found: " + args.toAccount;
  }
  var targetMailbox = findMailboxDeep(targetAccount, args.toMailbox);
  if (!targetMailbox) {
    throw "target mailbox not found: " + args.toMailbox +
      ' in account "' + targetAccount.name() + '"' +
      (args.toAccount ? "" : " (if the destination is in another account, pass to_account)");
  }
  handle.app.move(handle.message, { to: targetMailbox });
  return { moved: true, toMailbox: targetMailbox.name(), toAccount: targetAccount.name() };
}

function updateEmailStatus(args) {
  var handle = getMessageHandle(args);
  var msg = handle.message;
  if (args.action === "mark_read") msg.readStatus = true;
  else if (args.action === "mark_unread") msg.readStatus = false;
  else if (args.action === "flag") msg.flaggedStatus = true;
  else if (args.action === "unflag") msg.flaggedStatus = false;
  else if (args.action === "set_flag_color") {
    // flagIndex 0-6 selects one of Mail's seven colored flags; the Python
    // layer (core/flags.py) maps the color name to this integer. Setting it
    // also flags the message. -1 would unflag, but that path is "unflag".
    msg.flagIndex = args.flagIndex;
  } else throw "unknown status action: " + args.action;
  return { updated: true, action: args.action };
}

function manageTrash(args) {
  var handle = getMessageHandle(args);
  if (args.action === "move_to_trash") {
    var trashMailbox = findMailbox(handle.account, "trash");
    if (!trashMailbox) throw "trash mailbox not found for account " + handle.account.name();
    handle.app.move(handle.message, { to: trashMailbox });
    return { trashed: true };
  }
  if (args.action === "delete_permanent") {
    handle.app.delete(handle.message);
    return { deleted: true };
  }
  throw "unknown trash action: " + args.action;
}

function trashCount(args) {
  var app = MailApp();
  var account = findAccount(app, args.accountHint);
  if (!account) throw "account not found: " + args.accountHint;
  var trashMailbox = findMailbox(account, "trash");
  if (!trashMailbox) throw "trash mailbox not found";
  return { count: trashMailbox.messages().length };
}

function emptyTrash(args) {
  var app = MailApp();
  var account = findAccount(app, args.accountHint);
  if (!account) throw "account not found: " + args.accountHint;
  var trashMailbox = findMailbox(account, "trash");
  if (!trashMailbox) throw "trash mailbox not found";
  var messages = trashMailbox.messages();
  var count = messages.length;
  for (var i = count - 1; i >= 0; i--) {
    app.delete(messages[i]);
  }
  return { emptied: true, count: count };
}

// --- Mail rules -----------------------------------------------------------
// Mail's scripting dictionary exposes rules read/write for lifecycle (enable/
// disable/delete) but CANNOT create or modify rule *conditions* — `make` on a
// `rule condition` raises "Can't make or move that element into that
// container", so create/update of a functional rule is impossible via JXA.
// Verified live against a real Mail.app. Hence: read + lifecycle only.

function _ruleActionValue(fn, isMailbox) {
  try {
    var v = fn();
    if (v === null || v === undefined) return null;
    if (isMailbox) return v.name ? v.name() : null;
    return v;
  } catch (e) {
    return null;
  }
}

function _serializeRule(rule) {
  var out = { name: rule.name(), enabled: false, allConditionsMustBeMet: false, conditions: [], actions: {} };
  try { out.enabled = rule.enabled(); } catch (e) {}
  try { out.allConditionsMustBeMet = rule.allConditionsMustBeMet(); } catch (e) {}
  try {
    var conds = rule.ruleConditions();
    for (var i = 0; i < conds.length; i++) {
      var c = conds[i];
      out.conditions.push({
        ruleType: _ruleActionValue(function () { return c.ruleType(); }, false),
        qualifier: _ruleActionValue(function () { return c.qualifier(); }, false),
        expression: _ruleActionValue(function () { return c.expression(); }, false),
        header: _ruleActionValue(function () { return c.header(); }, false)
      });
    }
  } catch (e) {}
  var a = out.actions;
  a.shouldMoveMessage = _ruleActionValue(function () { return rule.shouldMoveMessage(); }, false);
  a.moveMessage = _ruleActionValue(function () { return rule.moveMessage(); }, true);
  a.shouldCopyMessage = _ruleActionValue(function () { return rule.shouldCopyMessage(); }, false);
  a.copyMessage = _ruleActionValue(function () { return rule.copyMessage(); }, true);
  a.markFlagged = _ruleActionValue(function () { return rule.markFlagged(); }, false);
  a.markFlagIndex = _ruleActionValue(function () { return rule.markFlagIndex(); }, false);
  a.colorMessage = _ruleActionValue(function () { return rule.colorMessage(); }, false);
  a.markRead = _ruleActionValue(function () { return rule.markRead(); }, false);
  a.deleteMessage = _ruleActionValue(function () { return rule.deleteMessage(); }, false);
  a.forwardMessage = _ruleActionValue(function () { return rule.forwardMessage(); }, false);
  a.forwardText = _ruleActionValue(function () { return rule.forwardText(); }, false);
  a.redirectMessage = _ruleActionValue(function () { return rule.redirectMessage(); }, false);
  a.replyText = _ruleActionValue(function () { return rule.replyText(); }, false);
  a.runScript = _ruleActionValue(function () { return rule.runScript(); }, false);
  a.playSound = _ruleActionValue(function () { return rule.playSound(); }, false);
  a.stopEvaluatingRules = _ruleActionValue(function () { return rule.stopEvaluatingRules(); }, false);
  return out;
}

function listRules(args) {
  var app = MailApp();
  var rules = app.rules();
  var out = [];
  for (var i = 0; i < rules.length; i++) {
    out.push(_serializeRule(rules[i]));
  }
  return { rules: out };
}

function _findRule(app, name) {
  var rules = app.rules();
  for (var i = 0; i < rules.length; i++) {
    if (rules[i].name() === name) return rules[i];
  }
  return null;
}

function setRuleEnabled(args) {
  var app = MailApp();
  var rule = _findRule(app, args.name);
  if (!rule) throw "rule not found: " + args.name;
  rule.enabled = !!args.enabled;
  return { name: rule.name(), enabled: rule.enabled() };
}

function deleteRule(args) {
  var app = MailApp();
  var rule = _findRule(app, args.name);
  if (!rule) throw "rule not found: " + args.name;
  app.delete(rule);
  return { deleted: true, name: args.name };
}

function createMailbox(args) {
  var app = MailApp();
  var account = findAccount(app, args.account);
  if (!account) throw "account not found: " + args.account;
  var parts = String(args.name).split("/");
  var parent = null;
  var created = null;
  for (var i = 0; i < parts.length; i++) {
    var siblings = parent ? parent.mailboxes() : account.mailboxes();
    var existing = null;
    for (var j = 0; j < siblings.length; j++) {
      if (siblings[j].name() === parts[i]) {
        existing = siblings[j];
        break;
      }
    }
    created = existing || app.Mailbox({ name: parts[i] }).make({ at: parent || account });
    parent = created;
  }
  return { created: true, name: created.name() };
}

// Recipients and attachments are constructed and PUSHED directly — never
// `.make()`'d. Verified live against a real Mail.app: `ToRecipient(...).make()`
// (and `Attachment(...).make()`) raise "-10024 Can't make or move that element
// into that container", exactly like rule conditions; the correct idiom is to
// push the constructed element onto the collection.
function addRecipients(outgoing, list, kind) {
  if (!list) return;
  var app = MailApp();
  for (var i = 0; i < list.length; i++) {
    var addr = list[i];
    if (kind === "to") outgoing.toRecipients.push(app.ToRecipient({ address: addr }));
    else if (kind === "cc") outgoing.ccRecipients.push(app.CcRecipient({ address: addr }));
    else if (kind === "bcc") outgoing.bccRecipients.push(app.BccRecipient({ address: addr }));
  }
}

function addAttachments(outgoing, paths) {
  if (!paths || paths.length === 0) return;
  var app = MailApp();
  for (var i = 0; i < paths.length; i++) {
    try {
      outgoing.content.attachments.push(app.Attachment({ fileName: paths[i] }));
    } catch (e) {
      // Fail loud: silently dropping an attachment the caller asked for
      // would mean we appear to succeed while sending something different
      // from what was requested.
      throw "failed to attach file " + paths[i] + ": " + e;
    }
  }
  // Mail loads the attachment file asynchronously; give it a moment to finish
  // before the message is sent/saved, or the send can race the attach.
  try { delay(1); } catch (e) { /* delay() unavailable in some contexts */ }
}

function finalizeOutgoing(outgoing, mode) {
  if (mode === "send") {
    // Keep the compose window hidden for a programmatic send. The subject is
    // set by the caller, so Mail won't pop its "no subject" confirmation
    // dialog (which would block the send indefinitely).
    outgoing.visible = false;
    outgoing.send();
    return "sent";
  }
  // "draft" and "open" both leave a compose window open for review — Mail's
  // scripting dictionary has no "save silently to Drafts without a window".
  outgoing.visible = true;
  return "draft";
}

// Resolve the address to send from: an explicit fromAddress wins; otherwise the
// requested account's own first address. Mail picks the sending account by the
// sender address, so without this a compose silently goes out from the default
// account regardless of the `account` argument (verified live: an explicit
// account argument still sent From the default account's address).
function _senderFor(app, args) {
  if (args.fromAddress) return args.fromAddress;
  if (args.account) {
    var acc = findAccount(app, args.account);
    if (acc) {
      var addrs = accountEmailAddresses(acc);
      if (addrs.length) return addrs[0];
    }
  }
  return null;
}

function composeEmail(args) {
  var app = MailApp();
  var outgoing = app.OutgoingMessage().make();
  outgoing.visible = false;
  // Set subject/content by ASSIGNMENT — the withProperties form on .make()
  // does NOT apply them (verified live: the subject came out empty and Mail
  // then blocked the send on a "no subject" dialog).
  outgoing.subject = args.subject || "";
  outgoing.content = args.body || "";
  var senderAddr = _senderFor(app, args);
  if (senderAddr) {
    try { outgoing.sender = senderAddr; } catch (e) {}
  }
  addRecipients(outgoing, args.to, "to");
  addRecipients(outgoing, args.cc, "cc");
  addRecipients(outgoing, args.bcc, "bcc");
  addAttachments(outgoing, args.attachments);
  return { status: finalizeOutgoing(outgoing, args.mode || "send") };
}

function replyToEmail(args) {
  var handle = getMessageHandle(args);
  var outgoing = handle.message.reply({ openingWindow: false, replyToAll: !!args.replyAll });
  if (args.body) {
    outgoing.content = args.body + "\n\n" + outgoing.content();
  }
  addRecipients(outgoing, args.cc, "cc");
  addRecipients(outgoing, args.bcc, "bcc");
  addAttachments(outgoing, args.attachments);
  return { status: finalizeOutgoing(outgoing, args.mode || "send") };
}

function forwardEmail(args) {
  var handle = getMessageHandle(args);
  var outgoing = handle.message.forward({ openingWindow: false });
  if (args.message) {
    outgoing.content = args.message + "\n\n" + outgoing.content();
  }
  addRecipients(outgoing, args.to, "to");
  addRecipients(outgoing, args.cc, "cc");
  addRecipients(outgoing, args.bcc, "bcc");
  addAttachments(outgoing, args.attachments);
  return { status: finalizeOutgoing(outgoing, args.mode || "send") };
}

function manageDrafts(args) {
  var app = MailApp();
  var account = findAccount(app, args.account);
  if (!account) throw "account not found: " + args.account;
  var draftsMailbox = findMailbox(account, "drafts");
  if (!draftsMailbox) throw "drafts mailbox not found";

  if (args.action === "list") {
    var msgs = draftsMailbox.messages();
    var result = [];
    for (var i = 0; i < msgs.length; i++) {
      result.push(describeMessage(msgs[i], account.name(), draftsMailbox.name()));
    }
    return { drafts: result };
  }

  if (args.action === "create") {
    var outgoing = app.OutgoingMessage().make();
    outgoing.visible = false;
    // Assignment, not withProperties (see composeEmail) — else subject/content
    // come out empty.
    outgoing.subject = args.subject || "";
    outgoing.content = args.body || "";
    var senderAddr = _senderFor(app, args);
    if (senderAddr) {
      try { outgoing.sender = senderAddr; } catch (e) {}
    }
    addRecipients(outgoing, args.to, "to");
    addRecipients(outgoing, args.cc, "cc");
    addRecipients(outgoing, args.bcc, "bcc");
    addAttachments(outgoing, args.attachments);
    outgoing.visible = true;  // show the draft window for review
    return { created: true };
  }

  // Unsent drafts have no reliable RFC822 Message-ID yet, so send/open/
  // delete locate the draft by subject within the Drafts mailbox only — a
  // much narrower, lower-risk scope than a whole-mailbox subject search.
  var draftMsgs = draftsMailbox.messages();
  var matches = [];
  for (var d = 0; d < draftMsgs.length; d++) {
    if (String(draftMsgs[d].subject()).indexOf(args.draftSubject) !== -1) {
      matches.push(draftMsgs[d]);
    }
  }
  if (matches.length === 0) throw "no draft found matching subject: " + args.draftSubject;
  if (matches.length > 1) {
    throw "ambiguous draft subject (" + matches.length + " matches): " + args.draftSubject;
  }
  var draft = matches[0];

  if (args.action === "delete") {
    app.delete(draft);
    return { deleted: true };
  }
  if (args.action === "open") {
    draft.openingWindow = true;
    return { opened: true };
  }
  if (args.action === "send") {
    throw "Mail's scripting dictionary has no direct send-existing-draft " +
      "action; open the draft and send it manually, or recreate it via compose_email";
  }
  throw "unknown drafts action: " + args.action;
}
