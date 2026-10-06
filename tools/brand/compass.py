"""The Sextant mark is the compass rose the sidebar and the panel header use (mdi:compass-rose), in the brand teal."""
import sys
TEAL = "#0F4C5C"
PATH = "M15 9L12 0L9 9L0 12L9 15L12 24L15 15L24 12L15 9M4 12L10 10L11 12H4M12 20L10 14L12 13V20M12 4L14 10L12 11V4M14 14L13 12H20L14 14M8.7 17.3L5 19L6.7 15.3L8.3 15.8L8.7 17.3M17.3 15.3L19 19L15.3 17.3L15.8 15.7L17.3 15.3M6.7 8.7L5 5L8.7 6.7L8.2 8.2L6.7 8.7M15.3 6.7L19 5L17.3 8.7L15.7 8.2L15.3 6.7Z"
def mark_svg(size=512, fill=TEAL):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size}" height="{size}"><path fill="{fill}" d="{PATH}"/></svg>'
if __name__ == "__main__":
    open(sys.argv[1], "w").write(mark_svg(int(sys.argv[2]) if len(sys.argv) > 2 else 512))
