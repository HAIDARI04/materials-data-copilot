/* Browser-local display preferences, independent of immutable measurements. */
const PlotPreferences = {
  read(key) {
    try {
      const saved = JSON.parse(localStorage.getItem(`materialsCopilot.plot.v1.${key}`));
      return saved?.version === 1 && saved.value && typeof saved.value === 'object' ? saved.value : null;
    } catch { return null; }
  },
  write(key, value) {
    try {
      localStorage.setItem(`materialsCopilot.plot.v1.${key}`, JSON.stringify({version: 1, value}));
      return true;
    } catch { return false; }
  },
  position(value) {
    return value && Number.isFinite(value.left) && Number.isFinite(value.top)
      && value.left >= 0 && value.top >= 0 ? {left: value.left, top: value.top} : null;
  },
};
if (typeof module !== 'undefined' && module.exports) module.exports = PlotPreferences;
