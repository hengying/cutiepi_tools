/*
 * hud-overlay.c —— 独立电池角标（**备用方案**）
 *
 * 电池状态默认显示在系统状态栏里（见 src/cutiepi_hud/tray.py，走 SNI 托盘协议）。
 * 只有当面板没有托盘图标区（hud/mode = overlay）时才需要这个小程序，
 * 安装时要显式加 --with-overlay 才会编译它。
 *
 * 为什么用 C 而不是 Python：Wayland 下窗口不能自己决定位置，必须走
 * layer-shell 协议。Debian/树莓派 OS 没有打包 gtk-layer-shell 的 Python
 * 绑定（没有 typelib），所以这里用一个小 C 程序直接调用 libgtk-layer-shell。
 * 同一个程序在 X11 下用普通窗口 + 定位，行为保持一致。
 *
 * 状态来源：一行一个 key=value 的小文件（由 cutiepi-hud 守护进程写）。
 * 这样 GUI 进程崩了也不影响守护进程，反之亦然。
 *
 * 编译：见 build.sh（需要 libgtk-3-dev，Wayland 定位需要 libgtk-layer-shell-dev）
 */

#include <gtk/gtk.h>
#include <cairo.h>
#include <pango/pangocairo.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef HAVE_LAYER_SHELL
#include <gtk-layer-shell.h>
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ------------------------------------------------------------------ */
/* 配置                                                                */
/* ------------------------------------------------------------------ */
static char *state_path = "/tmp/cutiepi-hud-state";
static int margin_top = 40;
static int margin_right = 8;
static double font_size = 11.0;

/* ------------------------------------------------------------------ */
/* 状态                                                                */
/* ------------------------------------------------------------------ */
typedef struct {
    int percent;   /* -1 表示未知 */
    int charging;
    int present;
    int valid;     /* 至少收到过一次有效读数 */
} HudState;

static HudState hud = { -1, 0, 0, 0 };

static GtkWidget *window = NULL;
static GtkWidget *area = NULL;
static gboolean layershell_active = FALSE;
static gboolean wayland_session = FALSE;
static gboolean use_alpha = TRUE;
static int current_width = 80;
static int current_height = 28;

/* ------------------------------------------------------------------ */
/* 绘图小工具                                                          */
/* ------------------------------------------------------------------ */
static void rounded_rect(cairo_t *cr, double x, double y, double w, double h, double r)
{
    if (w <= 0 || h <= 0)
        return;
    if (r > w / 2.0)
        r = w / 2.0;
    if (r > h / 2.0)
        r = h / 2.0;
    if (r < 0)
        r = 0;

    cairo_new_sub_path(cr);
    cairo_arc(cr, x + w - r, y + r, r, -M_PI / 2.0, 0);
    cairo_arc(cr, x + w - r, y + h - r, r, 0, M_PI / 2.0);
    cairo_arc(cr, x + r, y + h - r, r, M_PI / 2.0, M_PI);
    cairo_arc(cr, x + r, y + r, r, M_PI, 3.0 * M_PI / 2.0);
    cairo_close_path(cr);
}

static gboolean display_is_wayland(void)
{
    GdkDisplay *display = gdk_display_get_default();
    const char *type_name;
    if (!display)
        return FALSE;
    type_name = G_OBJECT_TYPE_NAME(display);
    return (type_name != NULL && g_strrstr(type_name, "Wayland") != NULL);
}

static void format_text(char *out, size_t size)
{
    if (!hud.valid || hud.percent < 0)
        g_strlcpy(out, "--%", size);
    else if (hud.percent > 100)
        g_strlcpy(out, "100%", size);
    else
        g_snprintf(out, size, "%d%%", hud.percent);
}

static PangoFontDescription *make_font(void)
{
    PangoFontDescription *desc = pango_font_description_new();
    pango_font_description_set_family(desc, "Sans");
    pango_font_description_set_weight(desc, PANGO_WEIGHT_BOLD);
    pango_font_description_set_absolute_size(desc, font_size * PANGO_SCALE);
    return desc;
}

/* 文字区域宽度（同一套字体，保证尺寸计算和绘制一致） */
static int measure_text_width(void)
{
    char text[16];
    PangoLayout *layout;
    PangoFontDescription *desc;
    int width = 0;
    int height = 0;

    format_text(text, sizeof(text));
    layout = gtk_widget_create_pango_layout(area, text);
    desc = make_font();
    pango_layout_set_font_description(layout, desc);
    pango_layout_get_pixel_size(layout, &width, &height);
    pango_font_description_free(desc);
    g_object_unref(layout);
    return width;
}

static int measure_text_height(void)
{
    char text[16];
    PangoLayout *layout;
    PangoFontDescription *desc;
    int width = 0;
    int height = 0;

    format_text(text, sizeof(text));
    layout = gtk_widget_create_pango_layout(area, "--%");
    desc = make_font();
    pango_layout_set_font_description(layout, desc);
    pango_layout_get_pixel_size(layout, &width, &height);
    pango_font_description_free(desc);
    g_object_unref(layout);
    return height;
}

/* ------------------------------------------------------------------ */
/* 状态文件                                                            */
/* ------------------------------------------------------------------ */
static gboolean read_key_int(const char *text, const char *key, int *out)
{
    const char *p = text;
    size_t len = strlen(key);

    while ((p = strstr(p, key)) != NULL) {
        if ((p == text || p[-1] == '\n') && p[len] == '=') {
            *out = atoi(p + len + 1);
            return TRUE;
        }
        p += len;
    }
    return FALSE;
}

/* 返回 TRUE 表示状态发生了变化 */
static gboolean read_state(void)
{
    char buffer[1024];
    size_t count;
    FILE *fp;
    HudState next = hud;
    gboolean changed = FALSE;
    int value = 0;

    fp = fopen(state_path, "r");
    if (!fp)
        return FALSE;
    count = fread(buffer, 1, sizeof(buffer) - 1, fp);
    fclose(fp);
    buffer[count] = '\0';

    if (read_key_int(buffer, "battery_percent", &value)) {
        next.percent = value;
        next.valid = (value >= 0);
    }
    if (read_key_int(buffer, "charging", &value))
        next.charging = value ? 1 : 0;
    if (read_key_int(buffer, "battery_present", &value))
        next.present = value ? 1 : 0;

    if (next.percent != hud.percent || next.charging != hud.charging ||
        next.valid != hud.valid || next.present != hud.present) {
        changed = TRUE;
    }
    hud = next;
    return changed;
}

/* ------------------------------------------------------------------ */
/* 尺寸与位置                                                          */
/* ------------------------------------------------------------------ */
static void position_x11(void)
{
    GdkScreen *screen;
    GdkRectangle rect;
    int x;
    int y;

    if (layershell_active || wayland_session || window == NULL)
        return;

    screen = gtk_window_get_screen(GTK_WINDOW(window));
    if (screen == NULL)
        return;

    gdk_screen_get_monitor_workarea(screen, 0, &rect);
    x = rect.x + rect.width - current_width - margin_right;
    y = rect.y + margin_top;
    if (x < rect.x)
        x = rect.x;
    if (y < rect.y)
        y = rect.y;
    gtk_window_move(GTK_WINDOW(window), x, y);
}

static void ensure_size(void)
{
    int text_width;
    int text_height;
    int pad_x = 8;
    int gap = 6;
    int icon_width = 26;
    int width;
    int height;

    if (area == NULL)
        return;

    text_width = measure_text_width();
    text_height = measure_text_height();

    height = text_height + 12;
    if (height < 26)
        height = 26;
    width = pad_x + icon_width + gap + text_width + pad_x;
    if (width < 62)
        width = 62;

    if (width != current_width || height != current_height) {
        current_width = width;
        current_height = height;
        /* 非 resizable 的窗口用 size-request 决定尺寸；area 和窗口都设一遍 */
        gtk_widget_set_size_request(area, width, height);
        gtk_widget_set_size_request(GTK_WIDGET(window), width, height);
        if (!layershell_active) {
            gtk_window_resize(GTK_WINDOW(window), width, height);
            position_x11();
        }
    }
}

/* ------------------------------------------------------------------ */
/* 绘制                                                                */
/* ------------------------------------------------------------------ */
static void draw_battery(cairo_t *cr, double x, double y, double w, double h,
                         int percent, int charging,
                         double red, double green, double blue)
{
    double cap_width = 2.6;
    double cap_height = h * 0.40;
    double body_width = w - cap_width - 1.5;
    double line = 1.7;
    double inner = 2.8;

    /* 正极触点 */
    rounded_rect(cr, x + body_width + 1.5, y + (h - cap_height) / 2.0,
                 cap_width, cap_height, 1.1);
    cairo_set_source_rgba(cr, red, green, blue, 0.85);
    cairo_fill(cr);

    /* 外壳 */
    rounded_rect(cr, x + line / 2.0, y + line / 2.0,
                 body_width - line, h - line, 3.0);
    cairo_set_line_width(cr, line);
    cairo_set_source_rgba(cr, red, green, blue, 0.95);
    cairo_stroke(cr);

    /* 电量填充 */
    if (percent >= 0) {
        double available = body_width - 2 * inner;
        double fill = available * (percent / 100.0);
        if (fill > 0.8) {
            rounded_rect(cr, x + inner, y + inner, fill, h - 2 * inner, 1.5);
            cairo_set_source_rgba(cr, red, green, blue, charging ? 0.35 : 0.90);
            cairo_fill(cr);
        }
    }

    /* 充电闪电 */
    if (charging) {
        double cx = x + body_width / 2.0;
        double cy = y + h / 2.0;
        double s = h * 0.80;
        cairo_move_to(cr, cx + 0.10 * s, cy - 0.50 * s);
        cairo_line_to(cr, cx - 0.30 * s, cy + 0.06 * s);
        cairo_line_to(cr, cx - 0.03 * s, cy + 0.06 * s);
        cairo_line_to(cr, cx - 0.12 * s, cy + 0.50 * s);
        cairo_line_to(cr, cx + 0.30 * s, cy - 0.06 * s);
        cairo_line_to(cr, cx + 0.03 * s, cy - 0.06 * s);
        cairo_close_path(cr);
        cairo_set_source_rgba(cr, 0.05, 0.10, 0.07, 0.92);
        cairo_fill(cr);
    }
}

static gboolean on_draw(GtkWidget *widget, cairo_t *cr, gpointer data)
{
    int width = gtk_widget_get_allocated_width(widget);
    int height = gtk_widget_get_allocated_height(widget);
    double red = 0.92, green = 0.95, blue = 0.96;
    double pad_x = 8.0;
    double icon_width = 26.0;
    double icon_height = 13.0;
    double icon_x = pad_x;
    double icon_y = (height - icon_height) / 2.0;
    char text[16];
    PangoLayout *layout;
    PangoFontDescription *desc;
    int text_width = 0;
    int text_height = 0;

    (void)data;

    /* 背景（Wayland / 有合成器的 X11 用透明） */
    if (use_alpha) {
        cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
        cairo_set_source_rgba(cr, 0, 0, 0, 0);
        cairo_paint(cr);
        cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
    } else {
        cairo_set_source_rgb(cr, 0.09, 0.09, 0.10);
        cairo_paint(cr);
    }

    /* 胶囊底 */
    if (use_alpha) {
        rounded_rect(cr, 0.5, 0.5, width - 1.0, height - 1.0, height / 2.0);
        cairo_set_source_rgba(cr, 0.04, 0.05, 0.07, 0.62);
        cairo_fill(cr);
    }

    /* 颜色：充电=绿，电量低=红/橙 */
    if (hud.charging) {
        red = 0.44; green = 0.93; blue = 0.55;
    } else if (hud.valid && hud.percent >= 0 && hud.percent <= 10) {
        red = 1.00; green = 0.44; blue = 0.40;
    } else if (hud.valid && hud.percent >= 0 && hud.percent <= 20) {
        red = 1.00; green = 0.72; blue = 0.30;
    }

    draw_battery(cr, icon_x, icon_y, icon_width, icon_height,
                 hud.valid ? hud.percent : -1, hud.charging,
                 red, green, blue);

    /* 文字 */
    format_text(text, sizeof(text));
    layout = pango_cairo_create_layout(cr);
    desc = make_font();
    pango_layout_set_font_description(layout, desc);
    pango_layout_set_text(layout, text, -1);
    pango_layout_get_pixel_size(layout, &text_width, &text_height);
    cairo_set_source_rgba(cr, red, green, blue, 1.0);
    cairo_move_to(cr, width - pad_x - text_width,
                  (height - text_height) / 2.0);
    pango_cairo_show_layout(cr, layout);
    pango_font_description_free(desc);
    g_object_unref(layout);

    return FALSE;
}

/* ------------------------------------------------------------------ */
/* 定时刷新                                                            */
/* ------------------------------------------------------------------ */
static gboolean on_tick(gpointer data)
{
    (void)data;
    if (read_state()) {
        ensure_size();
        if (area != NULL)
            gtk_widget_queue_draw(area);
    }
    if (!layershell_active)
        position_x11();
    return G_SOURCE_CONTINUE;
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */
int main(int argc, char **argv)
{
    GOptionEntry entries[] = {
        { "state-file", 0, 0, G_OPTION_ARG_STRING, &state_path,
          "状态文件路径", "PATH" },
        { "margin-top", 0, 0, G_OPTION_ARG_INT, &margin_top,
          "距屏幕上边缘的像素", "PX" },
        { "margin-right", 0, 0, G_OPTION_ARG_INT, &margin_right,
          "距屏幕右边缘的像素", "PX" },
        { "font-size", 0, 0, G_OPTION_ARG_DOUBLE, &font_size,
          "字号（像素）", "PX" },
        { NULL, 0, 0, 0, NULL, NULL, NULL }
    };
    GError *error = NULL;
    GdkScreen *screen;
    GdkVisual *visual;
    gboolean wayland;

    if (!gtk_init_with_args(&argc, &argv, "- CutiePi 电池角标", entries, NULL, &error)) {
        g_printerr("参数错误：%s\n", error ? error->message : "未知");
        if (error)
            g_error_free(error);
        return 1;
    }

    wayland = display_is_wayland();
    wayland_session = wayland;

    window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(window), "cutiepi-hud");
    gtk_window_set_decorated(GTK_WINDOW(window), FALSE);
    gtk_window_set_resizable(GTK_WINDOW(window), FALSE);
    gtk_window_set_skip_taskbar_hint(GTK_WINDOW(window), TRUE);
    gtk_window_set_skip_pager_hint(GTK_WINDOW(window), TRUE);
    gtk_window_set_accept_focus(GTK_WINDOW(window), FALSE);
    gtk_window_set_focus_on_map(GTK_WINDOW(window), FALSE);
    gtk_window_set_type_hint(GTK_WINDOW(window), GDK_WINDOW_TYPE_HINT_UTILITY);

    screen = gtk_window_get_screen(GTK_WINDOW(window));
    visual = gdk_screen_get_rgba_visual(screen);
    if (wayland) {
        use_alpha = TRUE;
    } else {
        use_alpha = (visual != NULL && gdk_screen_is_composited(screen));
    }
    if (visual != NULL && use_alpha)
        gtk_widget_set_visual(window, visual);
    gtk_widget_set_app_paintable(window, TRUE);

    area = gtk_drawing_area_new();
    gtk_widget_set_size_request(area, current_width, current_height);
    gtk_container_add(GTK_CONTAINER(window), area);
    g_signal_connect(area, "draw", G_CALLBACK(on_draw), NULL);

#ifdef HAVE_LAYER_SHELL
    if (wayland && gtk_layer_is_supported()) {
        gtk_layer_init_for_window(GTK_WINDOW(window));
        gtk_layer_set_namespace(GTK_WINDOW(window), "cutiepi-hud");
        gtk_layer_set_layer(GTK_WINDOW(window), GTK_LAYER_SHELL_LAYER_TOP);
        gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_TOP, TRUE);
        gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_RIGHT, TRUE);
        gtk_layer_set_margin(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_TOP, margin_top);
        gtk_layer_set_margin(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_RIGHT, margin_right);
        gtk_layer_auto_exclusive_zone_disable(GTK_WINDOW(window));
        gtk_layer_set_keyboard_mode(GTK_WINDOW(window),
                                    GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);
        layershell_active = TRUE;
    }
#endif

    if (!layershell_active) {
        gtk_window_set_keep_above(GTK_WINDOW(window), TRUE);
        gtk_window_stick(GTK_WINDOW(window));
    }

    g_signal_connect(window, "destroy", G_CALLBACK(gtk_main_quit), NULL);

    read_state();
    gtk_widget_show_all(window);
    ensure_size();
    if (!layershell_active)
        position_x11();

    g_timeout_add_seconds(1, on_tick, NULL);
    gtk_main();
    return 0;
}
