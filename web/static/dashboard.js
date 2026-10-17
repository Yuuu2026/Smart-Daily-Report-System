/*
 * 工作台前端增强：
 * 1) 进展草稿自动保存/恢复 —— 同步、刷新都不会丢失已填写内容
 * 2) 照片多选、累积选择、预览与数量提示
 * 3) 同步操作改为异步，只刷新活动流水区，不整页刷新
 */
(function () {
  'use strict';

  /* ============================================================
   * 1. 草稿自动保存与恢复
   * ============================================================ */
  var progressForm = document.querySelector('form.progress-card');
  if (progressForm) {
    var draftKey = 'daily-report-draft:' + (document.body.getAttribute('data-draft-key') || 'default');
    var TEXT_FIELDS = ['finished', 'problems', 'next_steps'];
    var ALL_FIELDS = ['work_date', 'report_detail'].concat(TEXT_FIELDS, [
      'sender_email', 'recipient_email', 'recipient_email_2', 'recipient_email_3'
    ]);

    function collectDraft() {
      var data = {};
      ALL_FIELDS.forEach(function (name) {
        var element = progressForm.elements[name];
        if (!element) return;
        if (element.type === 'file') return;
        data[name] = element.value;
      });
      return data;
    }

    var saveTimer = null;
    function saveDraft() {
      try {
        window.localStorage.setItem(draftKey, JSON.stringify(collectDraft()));
      } catch (error) {
        /* 隐私模式等场景下忽略 */
      }
    }
    function saveDraftSoon() {
      if (saveTimer) window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(saveDraft, 250);
    }

    progressForm.addEventListener('input', saveDraftSoon);
    progressForm.addEventListener('change', saveDraftSoon);
    window.addEventListener('pagehide', saveDraft);

    // 发布成功（页面重载后出现成功提示）时清理草稿
    if (document.querySelector('.flash.is-success')) {
      try {
        window.localStorage.removeItem(draftKey);
      } catch (error) {
        /* ignore */
      }
    }

    // 恢复草稿
    var stored = null;
    try {
      stored = window.localStorage.getItem(draftKey);
    } catch (error) {
      stored = null;
    }

    if (stored) {
      var restoredCount = 0;
      var draft = {};
      try {
        draft = JSON.parse(stored) || {};
      } catch (error) {
        draft = {};
      }

      TEXT_FIELDS.forEach(function (name) {
        var element = progressForm.elements[name];
        if (!element) return;
        if (!element.value && draft[name]) {
          element.value = draft[name];
          restoredCount += 1;
        }
      });

      if (restoredCount) {
        var hint = document.createElement('div');
        hint.className = 'draft-restore-hint';

        var label = document.createElement('span');
        label.textContent = '已恢复上次未提交的 ' + restoredCount + ' 项内容';

        var clearButton = document.createElement('button');
        clearButton.type = 'button';
        clearButton.textContent = '清空草稿';
        clearButton.addEventListener('click', function () {
          TEXT_FIELDS.forEach(function (name) {
            var element = progressForm.elements[name];
            if (element) element.value = '';
          });
          try {
            window.localStorage.removeItem(draftKey);
          } catch (error) {
            /* ignore */
          }
          saveDraft();
          hint.remove();
        });

        hint.appendChild(label);
        hint.appendChild(clearButton);
        progressForm.parentNode.insertBefore(hint, progressForm);
      }
    }

    progressForm.addEventListener('submit', saveDraft);
  }

  /* ============================================================
   * 2. 照片多选：累积选择 + 预览 + 上限提示
   * ============================================================ */
  var photoInput = document.getElementById('attachments');
  if (photoInput) {
    var MAX_PHOTOS = 6;
    var picked = [];

    var previewList = document.createElement('div');
    previewList.className = 'photo-pick-list';

    var previewTip = document.createElement('p');
    previewTip.className = 'photo-pick-tip';

    photoInput.insertAdjacentElement('afterend', previewList);
    previewList.insertAdjacentElement('afterend', previewTip);

    function applyPicked() {
      try {
        var transfer = new DataTransfer();
        picked.forEach(function (file) {
          transfer.items.add(file);
        });
        photoInput.files = transfer.files;
      } catch (error) {
        /* 浏览器不支持 DataTransfer 时保留原生选择结果 */
      }
    }

    function renderPicked() {
      previewList.textContent = '';
      picked.forEach(function (file, index) {
        var item = document.createElement('span');
        item.className = 'photo-pick-item';

        var thumb = document.createElement('img');
        thumb.alt = file.name;
        try {
          thumb.src = URL.createObjectURL(file);
        } catch (error) {
          /* ignore */
        }

        var caption = document.createElement('em');
        caption.textContent = file.name.length > 12 ? file.name.slice(0, 10) + '…' : file.name;

        var removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.textContent = '×';
        removeButton.title = '移除这张照片';
        removeButton.setAttribute('aria-label', '移除 ' + file.name);
        removeButton.addEventListener('click', function () {
          picked.splice(index, 1);
          applyPicked();
          renderPicked();
        });

        item.appendChild(thumb);
        item.appendChild(caption);
        item.appendChild(removeButton);
        previewList.appendChild(item);
      });

      if (picked.length) {
        previewTip.textContent = '已选择 ' + picked.length + ' / ' + MAX_PHOTOS + ' 张，可继续添加或点击 × 移除';
      } else {
        previewTip.textContent = '可一次多选，也可分批添加，最多 ' + MAX_PHOTOS + ' 张';
      }
    }

    photoInput.addEventListener('change', function () {
      var incoming = Array.prototype.slice.call(photoInput.files || []);
      var rejected = 0;

      incoming.forEach(function (file) {
        if (picked.length >= MAX_PHOTOS) {
          rejected += 1;
          return;
        }
        if (file.type && file.type.indexOf('image/') !== 0) {
          rejected += 1;
          return;
        }
        var duplicated = picked.some(function (item) {
          return item.name === file.name && item.size === file.size;
        });
        if (duplicated) return;
        picked.push(file);
      });

      applyPicked();
      renderPicked();

      if (rejected) {
        previewTip.textContent = '已达 ' + MAX_PHOTOS + ' 张上限或存在非图片文件，已忽略 ' + rejected + ' 个';
      }
    });

    renderPicked();
  }

  /* ============================================================
   * 3. 同步不整页刷新：只替换活动流水区域
   * ============================================================ */
  var syncForm = document.querySelector('.sync-form');
  var activitySection = document.getElementById('activity-stream');

  if (syncForm && activitySection && window.fetch) {
    syncForm.addEventListener('submit', function (event) {
      event.preventDefault();

      var button = syncForm.querySelector('button');
      var originalLabel = button ? button.textContent : '';
      if (button) {
        button.disabled = true;
        button.textContent = '同步中…';
      }

      var payload = new FormData(syncForm);

      window.fetch(syncForm.action, {
        method: 'POST',
        body: payload,
        credentials: 'same-origin'
      })
        .then(function () {
          return window.fetch(window.location.href, { credentials: 'same-origin' });
        })
        .then(function (response) {
          return response.text();
        })
        .then(function (html) {
          var parsed = new DOMParser().parseFromString(html, 'text/html');
          var fresh = parsed.getElementById('activity-stream');
          if (fresh) {
            activitySection.innerHTML = fresh.innerHTML;
          }
        })
        .catch(function () {
          // 异步失败时回退为普通表单提交，保证功能可用
          syncForm.submit();
          return;
        })
        .then(function () {
          if (button) {
            button.disabled = false;
            button.textContent = originalLabel || '立即同步';
          }
        });
    });
  }
})();
