/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./public/**/*.html", "./public/**/*.js"],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        appleDark: '#0d0d0d',
        appleGray: '#1c1c1e',
        appleMuted: '#86868b',
      }
    },
  },
  plugins: [],
}
