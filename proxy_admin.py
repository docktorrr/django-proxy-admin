# -*- coding: utf-8 -*-
"""
Model admin interface that can display different add/change pages depending on the existing proxies for the model.
The idea of implementation comes from django-polymorphic model admin.
"""
from django.contrib import admin
from django import forms
from django.conf.urls import url
from django.contrib.admin.widgets import AdminRadioSelect
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template.context import RequestContext
from django.contrib.admin.templatetags.admin_urls import add_preserved_filters
from django.contrib.admin.helpers import AdminForm, AdminErrorList
from django.core.urlresolvers import RegexURLResolver
from django.core.exceptions import PermissionDenied
from django.utils.encoding import force_text
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
import django


class ProxyChoiceForm(forms.Form):
    """
    The default form for the ``add_type_form``. Can be overwritten and replaced.
    """
    type_label = _('Type')

    type = forms.ChoiceField(label=type_label, widget=AdminRadioSelect(attrs={'class': 'radiolist'}))

    def __init__(self, *args, **kwargs):
        super(ProxyChoiceForm, self).__init__(*args, **kwargs)
        self.fields['type'].label = self.type_label


class ParentModelAdmin(admin.ModelAdmin):
    """
    To use this class, three variables need to be defined:
    * :attr:`base_model`
    * :attr:`child_models` - a dictionary: key is type value (that defines proxy),  value is (Model, Admin) tuple
    * :attr:`type_field_name` - a name of the field that defines type (proxy)
    """
    base_model = None
    child_models = None
    type_field_name = None
    add_type_template = None
    add_type_form = ProxyChoiceForm

    def __init__(self, model, admin_site, *args, **kwargs):
        super(ParentModelAdmin, self).__init__(model, admin_site, *args, **kwargs)
        self._child_admin_site = self.admin_site.__class__(name=self.admin_site.name)
        self._is_setup = False

    def get_child_models(self):
        if self.child_models is None:
            raise NotImplementedError("Implement get_child_models() or child_models")
        return self.child_models

    def register_child(self, model, model_admin):
        if self._is_setup:
            raise RuntimeError("The admin model can't be registered anymore at this point.")
        if not issubclass(model, self.base_model):
            raise TypeError("{0} should be a subclass of {1}".format(model.__name__, self.base_model.__name__))
        if not issubclass(model_admin, admin.ModelAdmin):
            raise TypeError("{0} should be a subclass of {1}".format(model_admin.__name__, admin.ModelAdmin.__name__))
        self._child_admin_site.register(model, model_admin)

    def _lazy_setup(self):
        if self._is_setup:
            return

        child_models = self.get_child_models()
        for _, (Model, Admin) in child_models:
            self.register_child(Model, Admin)
        self._child_models = dict(child_models)

        complete_registry = self.admin_site._registry.copy()
        complete_registry.update(self._child_admin_site._registry)

        self._child_admin_site._registry = complete_registry
        self._is_setup = True

    def get_child_type_choices(self):
        choices = []
        for key, (model, _) in self.get_child_models():
            choices.append((key, model._meta.verbose_name))
        return choices

    def _get_real_admin(self, type_value):
        try:
            model, _ = self._child_models[type_value]
        except IndexError:
            raise Http404
        try:
            return self._child_admin_site._registry[model]
        except KeyError:
            raise RuntimeError("No child admin site was registered for a '{0}' model.".format(model))

    def add_view(self, request, form_url='', extra_context=None):
        type_value = int(request.GET.get('type', 0))
        if not type_value:
            return self.add_type_view(request)
        else:
            real_admin = self._get_real_admin(type_value)
            form_url = add_preserved_filters({
                'preserved_filters': urlencode({'type': type_value}),
                'opts': self.model._meta},
                form_url
            )
            return real_admin.add_view(request, form_url, extra_context)

    def _get_type_by_object_id(self, object_id):
        try:
            obj = self.base_model.objects.get(id=object_id)
        except self.base_model.DoesNotExist:
            raise Http404
        try:
            return getattr(obj, self.type_field_name)
        except AttributeError:
            raise NotImplementedError("Implement type_field_name")

    def change_view(self, request, object_id, *args, **kwargs):
        real_admin = self._get_real_admin(self._get_type_by_object_id(object_id))
        return real_admin.change_view(request, object_id, *args, **kwargs)

    if django.VERSION >= (1, 7):
        def changeform_view(self, request, object_id=None, *args, **kwargs):
            if object_id:
                real_admin = self._get_real_admin(self._get_type_by_object_id(object_id))
                return real_admin.changeform_view(request, object_id, *args, **kwargs)
            else:
                return super(ParentModelAdmin, self).changeform_view(request, object_id, *args, **kwargs)

    def get_urls(self):
        urls = super(ParentModelAdmin, self).get_urls()
        info = (self.model._meta.app_label, self.model._meta.model_name)
        if django.VERSION < (1, 9):
            new_change_url = url(
                r'^{0}/$'.format('(\d+|__fk__)'),
                self.admin_site.admin_view(self.change_view),
                name='{0}_{1}_change'.format(*info)
            )
            redirect_urls = []
            for i, oldurl in enumerate(urls):
                if oldurl.name == new_change_url.name:
                    urls[i] = new_change_url
        else:
            redirect_urls = [pat for pat in urls if not pat.name]  # redirect URL has no name.
            urls = [pat for pat in urls if pat.name]

        custom_urls = [
            url(r'^(?P<path>.+)$', self.admin_site.admin_view(self.subclass_view))
        ]

        self._lazy_setup()

        dummy_urls = []
        for type, _ in self.get_child_models():
            admin = self._get_real_admin(type)
            dummy_urls += admin.get_urls()

        return urls + custom_urls + dummy_urls + redirect_urls

    def subclass_view(self, request, path):
        type = int(request.GET.get('type', 0))
        if not type:
            try:
                pos = path.find('/')
                if pos == -1:
                    object_id = long(path)
                else:
                    object_id = long(path[0:pos])
            except ValueError:
                raise Http404("No type parameter, unable to find admin subclass for path '{0}'.".format(path))
            type = self._get_type_by_object_id(object_id)

        real_admin = self._get_real_admin(type)
        resolver = RegexURLResolver('^', real_admin.urls)
        resolvermatch = resolver.resolve(path)
        if not resolvermatch:
            raise Http404("No match for path '{0}' in admin subclass.".format(path))

        return resolvermatch.func(request, *resolvermatch.args, **resolvermatch.kwargs)

    def add_type_view(self, request, form_url=''):
        if not self.has_add_permission(request):
            raise PermissionDenied

        extra_qs = ''
        if request.META['QUERY_STRING']:
            extra_qs = '&' + request.META['QUERY_STRING']

        choices = self.get_child_type_choices()
        if len(choices) == 1:
            return HttpResponseRedirect('?type={0}{1}'.format(choices[0][0], extra_qs))

        form = self.add_type_form(
            data=request.POST if request.method == 'POST' else None,
            initial={'type': choices[0][0]}
        )
        form.fields['type'].choices = choices

        if form.is_valid():
            return HttpResponseRedirect('?type={0}{1}'.format(form.cleaned_data['type'], extra_qs))

        fieldsets = ((None, {'fields': ('type',)}),)
        admin_form = AdminForm(form, fieldsets, {}, model_admin=self)
        media = self.media + admin_form.media
        opts = self.model._meta

        context = {
            'title': _('Add %s') % force_text(opts.verbose_name),
            'adminform': admin_form,
            'is_popup': ("_popup" in request.POST or
                         "_popup" in request.GET),
            'media': mark_safe(media),
            'errors': AdminErrorList(form, ()),
            'app_label': opts.app_label,
        }
        return self.render_add_type_form(request, context, form_url)

    def render_add_type_form(self, request, context, form_url=''):
        opts = self.model._meta
        app_label = opts.app_label
        context.update({
            'has_change_permission': self.has_change_permission(request),
            'form_url': mark_safe(form_url),
            'opts': opts,
            'add': True,
            'save_on_top': self.save_on_top,
        })
        context_instance = RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(self.add_type_template or [
            "admin/%s/%s/add_type_form.html" % (app_label, opts.object_name.lower()),
            "admin/%s/add_type_form.html" % app_label,
            "admin/add_type_form.html"
        ], context, context_instance=context_instance)


class ChildModelAdmin(admin.ModelAdmin):
    """
    Optional base class for admin interface for proxy models
    It corrects the breadcrumb and help to customize some features
    """
    #TODO: Implement

    def __init__(self, model, admin_site, *args, **kwargs):
        raise NotImplemented()

