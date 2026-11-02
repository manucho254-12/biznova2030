const CHECK_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';

function likePhoto(photoId, btn) {
  fetch(`/photos/${photoId}/like`, { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        btn.outerHTML = `<span class="badge-done">${CHECK_SVG} Liked &mdash; Earned!</span>`;
        const balEl = document.getElementById('wallet-balance');
        if (balEl && data.new_balance !== undefined) {
          balEl.textContent = 'KES ' + data.new_balance.toFixed(2);
        }
      } else {
        alert(data.message);
      }
    })
    .catch(() => alert('Something went wrong. Please try again.'));
}

function watchVideo(videoId, btn) {
  btn.disabled = true;
  btn.textContent = 'Watching... please wait';
  setTimeout(() => {
    fetch(`/videos/${videoId}/watch`, { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          btn.outerHTML = `<span class="badge-done">${CHECK_SVG} Watched &mdash; Earned!</span>`;
          const balEl = document.getElementById('wallet-balance');
          if (balEl && data.new_balance !== undefined) {
            balEl.textContent = 'KES ' + data.new_balance.toFixed(2);
          }
        } else {
          alert(data.message);
          btn.disabled = false;
          btn.textContent = 'Mark as Watched';
        }
      })
      .catch(() => {
        alert('Something went wrong. Please try again.');
        btn.disabled = false;
      });
  }, 3000); // require at least 3s of "watch time" before crediting
}

function copyReferralLink(el) {
  const text = el.getAttribute('data-link');
  navigator.clipboard.writeText(text).then(() => {
    const original = el.innerHTML;
    el.textContent = 'Copied!';
    setTimeout(() => { el.innerHTML = original; }, 1500);
  });
}
