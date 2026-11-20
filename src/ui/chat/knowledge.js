// "O que a casa sabe de ti" is a page of its own now (/conhecimento). Anything that still asks for it
// with the old window event ("tt:knowledge") is sent there.
window.addEventListener("tt:knowledge", () => { location.href = "/conhecimento"; });
