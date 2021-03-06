from io import StringIO

from gi.repository import GObject
from gi.repository import Gdk
from gi.repository import Gtk
from gi.repository import GtkFlow

import pinmagik.nodes

from pinmagik.nodes.source import Source

from pinmagik.raspi import RaspiContext, RaspiInNode, RaspiOutNode

import json

# Placeholder function for gettext
def _(string):
    return string

try:
    import RPi.GPIO as GPIO
    IS_REAL_RASPI = True
except ImportError:
    IS_REAL_RASPI = False

PD_FIELD_ID = 0
PD_FIELD_NAME = 1
PD_FIELD_HUMAN_NAME = 2
PROJECT_TYPES = {
    "raspi" :      (0x01, "raspi",      _("Raspberry Pi Model A/B")),
    "raspi_plus" : (0x02, "raspi_plus", _("Raspberry Pi Model A+/B+")),
}

class Compiler(object):
    FRAME = """#!/usr/bin/python3
#
# *** Generated by Pinmagic ***
#

from sys import exit
from time import sleep
import RPi.GPIO as GPIO

def init():
    GPIO.setmode(GPIO.BCM)
%(INIT)s

def loopstep():
%(LOOP)s

init()

if not __name__ == "__main__":
    exit(0) 

try:
    while True:
        sleep(0.01)
        loopstep()
except KeyboardInterrupt:
    GPIO.cleanup()
"""

    def __init__(self, project):
        self._project = project
        self._visited_nodes_init = []
        self._visited_nodes_loop = []
        self._init_buffer = StringIO()
        self._loop_buffer = StringIO()

    def compile(self):
        self._visited_nodes_init = []
        self._visited_nodes_loop = []
        self._init_buffer = StringIO()
        self._loop_buffer = StringIO()
        start_node = self._project.get_output_node()
        start_node.generate_raspi_init(self)
        start_node.generate_raspi_loop(self)

        code = Compiler.FRAME%{"INIT": self._init_buffer.getvalue(),
                               "LOOP": self._loop_buffer.getvalue()}
        return code

    def get_init_buffer(self):
        return self._init_buffer

    def get_loop_buffer(self):
        return self._loop_buffer

    def rendered_as_init(self, node):
        return node in self._visited_nodes_init

    def rendered_as_loop(self, node):
        return node in self._visited_nodes_loop

    def set_rendered_init(self, node):
        self._visited_nodes_init.append(node)

    def set_rendered_loop(self, node):
        self._visited_nodes_loop.append(node)

class Serializer(object):
    def __init__(self, project):
        self._project = project
        self._visited_nodes = []
        self._remaining_nodes = project.get_nodes()
        self._serialized_nodes = []

    def serialize_node(self,node):
        cons = []
        sinks = node.get_sinks()
        for sink in sinks:
            source = sink.get_source()
            if source is not None:
                targetnode = source.get_node()
                sources = targetnode.get_sources()
                cons.append((sinks.index(sink), id(targetnode), sources.index(source)))
        alloc = PinMagic.INSTANCE.nodeview.get_node_allocation(node)
        return {
            "clsid": node.__class__.ID,
            "x": alloc.x,
            "y": alloc.y,
            "node_info": {},
            "id": id(node),
            "connections": cons
        }

    def serialize(self):
        startnode = self._project.get_output_node()
        startnode.serialize(self)
        while len(self._remaining_nodes) > 0:
            startnode = self._remaining_nodes[0]
            startnode.serialize(self)
        return json.dumps({
            "type":self._project.get_type(),
            "nodes":self._serialized_nodes
        })

    def is_serialized(self, node):
        return node in self._visited_nodes

    def set_serialized(self, node, serialized):
        self._serialized_nodes.append(serialized)
        self._visited_nodes.append(node)
        self._remaining_nodes.remove(node)

class Deserializer(object):
    def __init__(self, project, json_data):
        self._data = json.loads(json_data)
        self._project = project

    def deserialize(self):
        rc = RaspiContext(RaspiContext.REV_1)
        node_id_map = {}
        in_node = out_node = None
        for nd in self._data["nodes"]:
            if not nd["clsid"] in PinMagic.NODE_INDEX:
                return
            node_cls = PinMagic.NODE_INDEX[nd["clsid"]]
            if node_cls is None:
                return

            
            if nd["clsid"] in (0x8001, 0x8002):
                new_node = node_cls(rc)
            else:
                new_node = node_cls()
            node_id_map[nd["id"]] = new_node
            new_node.deserialize(nd["node_info"])
            if new_node.__class__.ID in (0x8001, 0x8002):
                new_node.add_to_nodeview(PinMagic.S().nodeview)
            elif hasattr(new_node, "childwidget") and new_node.childwidget:
                PinMagic.S().nodeview.add_with_child(new_node, new_node.childwidget)
            else:
                PinMagic.S().nodeview.add_node(new_node)

            if new_node.__class__.ID == 0x8001:
                in_node = new_node
            elif new_node.__class__.ID == 0x8002:
                out_node = new_node
            else:
                self._project.get_nodes().append(new_node)
            PinMagic.S().nodeview.set_node_position(new_node, nd["x"], nd["y"])
            PinMagic.S().nodeview.set_show_types(False)

        self._project.get_nodes().insert(0, in_node)
        self._project.get_nodes().insert(1, out_node)

        for nd in self._data["nodes"]:
            node = node_id_map[nd["id"]]
            for con in nd["connections"]:
                target = node_id_map[con[1]]
                node.get_sinks()[con[0]].link(target.get_sources()[con[2]])

class Project(object):
    def __init__(self, typ):
        self._type = typ
        self._nodes = []
        self._filename = None

    def get_node_by_id(id_):
        for node in self._nodes:
            if id(node) == id_:
                return node
        return None

    def compile(self):
        return Compiler(self).compile()

    def set_filename(self, fn):
        self._filename = fn

    def get_filename(self):
        return self._filename

    def get_nodes(self):
        return self._nodes

    def set_nodes(self, nodes):
        self._nodes = nodes

    def get_type(self):
        return self._type

    def get_output_node(self):
        return self._nodes[1]

    def serialize(self):
        return Serializer(self).serialize()

    def deserialize(self, json_data):
        Deserializer(self, json_data).deserialize()

class PinMagic(object):
    NODE_INDEX = {}
    INSTANCE = None
    @staticmethod
    def get_node_classes():
        ret = []
        for x in dir(pinmagik.nodes):
            if not x.startswith("_") and x not in pinmagik.nodes.EXCLUDES:
                exec("ret.append(pinmagik.nodes.%s)"%(x,))
        return ret

    @classmethod
    def build_node_index(cls):
        ret = []
        for x in dir(pinmagik.nodes):
            if not x.startswith("_") and x not in pinmagik.nodes.EXCLUDES:
                exec("cls.NODE_INDEX[pinmagik.nodes.%s.ID] = pinmagik.nodes.%s"%(x,x))
        cls.NODE_INDEX[RaspiOutNode.ID] = RaspiOutNode
        cls.NODE_INDEX[RaspiInNode.ID] = RaspiInNode

    @classmethod
    def S(cls):
        if cls.INSTANCE is None:
            cls.INSTANCE = PinMagic()
        return cls.INSTANCE

    def __init__(self):
        Gtk.init([])

        PinMagic.build_node_index()

        self._current_project = None

        self.headerbar = Gtk.HeaderBar.new()
        self.headerbar.set_title("PinMagic")
        self.headerbar.set_subtitle("untitled")

        self.nodeview = GtkFlow.NodeView.new()
        self.nodeview.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.nodeview.connect("drag-data-received", self.on_new_node)
        self.nodeview.drag_dest_add_text_targets()

        self.builder = Gtk.Builder.new()
        self.builder.add_from_file("main.ui")
        self.window = self.builder.get_object("window")
        self.scrollarea = self.builder.get_object("scrolledwindow")
        self.nodestree = self.builder.get_object("nodestreeview")
        box = self.builder.get_object("box")
        revealer = self.builder.get_object("info_revealer")
        revealer.set_reveal_child(False)
        box.pack_start(self.headerbar, False, True, 0)
        box.reorder_child(self.headerbar,0)
        self.scrollarea.add_with_viewport(self.nodeview)

        crt = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("Toolbox"), crt, text=0)
        self.nodestree.append_column(col)
        self.nodestree.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, [],
                                                Gdk.DragAction.COPY)
        self.nodestree.connect("drag-data-get", self.on_drag_toolbox)
        self.nodestree.drag_source_add_text_targets()

        self.export = Gtk.Button.new_from_icon_name("document-send", Gtk.IconSize.BUTTON)
        self.export.connect("clicked", self.on_export)
        self.headerbar.pack_end(self.export)

        self.new = Gtk.MenuButton.new()
        self.new.set_popup(self._build_new_menu())
        self.new.set_image(Gtk.Image.new_from_icon_name("document-new", Gtk.IconSize.BUTTON))
        self.headerbar.pack_start(self.new)

        self.save = Gtk.Button.new_from_icon_name("document-save", Gtk.IconSize.BUTTON)
        self.save.connect("clicked", self.on_save)
        self.headerbar.pack_start(self.save)

        self.open = Gtk.Button.new_from_icon_name("document-open", Gtk.IconSize.BUTTON)
        self.open.connect("clicked", self.on_load)
        self.headerbar.pack_start(self.open)

        self.live = None
        if IS_REAL_RASPI:
            self.live = Gtk.Button.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
            self.headerbar.pack_end(self.live)

        self.window.show_all()
        self.window.connect("destroy", self.quit)

        self.update_ui()

        PinMagic.get_node_classes()

    def on_drag_toolbox(self, widget, darg_context, data, info, time):
        selected_path = self.nodestree.get_selection().get_selected_rows()[1][0]
        if len(selected_path) < 2:
            return
        m = self.nodestree.get_model()
        treeiter = m.get_iter(selected_path)
        data.set_text("node_"+str(m.get_value(treeiter,1)),-1)

    def on_new_node(self, widget, drag_context, x, y, data, info, time):
        txt = data.get_text()
        if txt is None or not txt.startswith("node_"):
            return

        node_cls_id = int(txt.replace("node_","",1))
        if not node_cls_id in PinMagic.NODE_INDEX:
            return
        node_cls = PinMagic.NODE_INDEX[node_cls_id]
        if node_cls is None:
            return

        new_node = node_cls()
        if hasattr(new_node, "childwidget") and new_node.childwidget:
            self.nodeview.add_with_child(new_node, new_node.childwidget)
        else:
            self.nodeview.add_node(new_node)
        self._current_project.get_nodes().append(new_node)
        self.nodeview.set_node_position(new_node, x, y)
        self.nodeview.set_show_types(False)

    def _build_new_model(self):
        if self._current_project:
            store = Gtk.TreeStore.new([GObject.TYPE_STRING,GObject.TYPE_INT])
            categories = {}
            for node in PinMagic.get_node_classes():
                if not pinmagik.nodes.supports(
                        node, self._current_project.get_type()[PD_FIELD_NAME]):
                    continue
                if not node.CATEGORY in categories:
                    categories[node.CATEGORY] = store.append(None, (node.CATEGORY,-1))
                store.append(categories[node.CATEGORY],(node.HR_NAME,node.ID))
        else:
            store = None
        self.nodestree.set_model(store)
        

    def _build_new_menu(self):
        menu = Gtk.Menu.new()
        for descriptor in PROJECT_TYPES.values():
            i = Gtk.MenuItem.new_with_label(descriptor[PD_FIELD_HUMAN_NAME])
            i.connect("activate", self.new_project, descriptor[PD_FIELD_NAME])
            menu.add(i)
        menu.show_all()
        return menu 
        
    def update_ui(self):
        has_project = self._current_project is not None
        self.export.set_sensitive(has_project)
        self.save.set_sensitive(has_project)
        self.scrollarea.set_sensitive(has_project)
        self.nodeview.set_sensitive(has_project)
        if has_project:
            if self._current_project.get_filename() is not None:
                self.headerbar.set_subtitle(self._current_project.get_filename())
            else:
                self.headerbar.set_subtitle(_("untitled"))
        else:
            self.headerbar.set_subtitle("")
        if self.live:
            self.live.set_sensitive(has_project)
            self.live.set_visible(self._current_project.get_type()[PD_FIELDNMAE] == "raspi")
        self._build_new_model()

    def on_export(self, widget=None, data=None):
        if self._current_project:
            dialog = Gtk.FileChooserDialog(_("Choose a filename"), self.window,
                Gtk.FileChooserAction.SAVE,
                (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                 Gtk.STOCK_SAVE, Gtk.ResponseType.OK))

            #self.window.add_filters(dialog)

            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                code = self._current_project.compile()
                f = open(dialog.get_filename(),"w")
                f.write(code)
                f.close()
            dialog.destroy()

    def on_save(self, widget=None, data=None):
        if self._current_project:
            dialog = Gtk.FileChooserDialog(_("Choose a filename"), self.window,
                Gtk.FileChooserAction.SAVE,
                (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                 Gtk.STOCK_SAVE, Gtk.ResponseType.OK))

            filt = Gtk.FileFilter()
            filt.set_name(_("PinMagic Projects"))
            filt.add_pattern("*.pimp")
            dialog.add_filter(filt)

            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                json_data = self._current_project.serialize()
                f = open(dialog.get_filename(),"w")
                f.write(json_data)
                f.close()
                self._current_project.set_filename(dialog.get_filename())
                self.update_ui()
            dialog.destroy()

    def on_load(self, widget=None, data=None):
        dialog = Gtk.FileChooserDialog(_("Choose a filename"), self.window,
            Gtk.FileChooserAction.SAVE,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
             Gtk.STOCK_OPEN, Gtk.ResponseType.OK))

        filt = Gtk.FileFilter()
        filt.set_name(_("PinMagic Projects"))
        filt.add_pattern("*.pimp")
        dialog.add_filter(filt)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            f = open(dialog.get_filename(),"r")
            json_data = f.read()
            f.close()
            self.nodeview.set_sensitive(True)
            self._clear_current_project()
            self._current_project = Project(json.loads(json_data)["type"])
            self._current_project.set_filename(dialog.get_filename())
            self.update_ui()
            self._current_project.deserialize(json_data)
        dialog.destroy()
        

    def _clear_current_project(self):
        if self._current_project:
            for node in self._current_project.get_nodes():
                self.nodeview.remove_node(node)

    def new_project(self, widget=None, data=None):
        self._clear_current_project()
        self._current_project = Project(PROJECT_TYPES[data])
        self.update_ui()

        rc = RaspiContext(RaspiContext.REV_1)
        rin = RaspiInNode(rc)
        rin.add_to_nodeview(self.nodeview)
        ron = RaspiOutNode(rc)
        ron.add_to_nodeview(self.nodeview)
        self.nodeview.set_node_position(rin, 1, 1)
        self.nodeview.set_node_position(ron, 600, 1)
        self._current_project.get_nodes().append(rin)
        self._current_project.get_nodes().append(ron)

    def load_project(self, project):
        self._clear_current_project()
        self._current_project = project
        self.update_ui()

    def quit(self, widget=None, data=None):
        Gtk.main_quit()

    @staticmethod
    def run():
        pm = PinMagic.S()
        Gtk.main()
