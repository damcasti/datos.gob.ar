from ckan.controllers.package \
    import PackageController, _encode_params, search_url, render, NotAuthorized, check_access, abort, get_action, log
from urllib import urlencode
from pylons import config
from paste.deploy.converters import asbool
from ckan.lib.render import deprecated_lazy_render
import ckan.lib.maintain as maintain
import ckan.lib.helpers as h
import ckan.model as model
import ckan.plugins as p
from ckan.common import OrderedDict, _, request, c, g
import ckan.logic as logic
from ckan.lib.search import SearchError
import ckan.lib.navl.dictization_functions as dict_fns
import ckan.lib.base as base

CACHE_PARAMETERS = ['__cache', '__no_cache__']
NotFound = logic.NotFound
ValidationError = logic.ValidationError
redirect = base.redirect
tuplize_dict = logic.tuplize_dict
clean_dict = logic.clean_dict
parse_params = logic.parse_params


def collect_descendants(organization_list):
    partial = []
    for organization in organization_list:
        partial.append(organization['name'])
        if 'children' in organization and len(organization['children']) > 0:
            partial += collect_descendants(organization['children'])
    return partial


def search_organization(organization_name, organizations_branch=None):
    if organizations_branch is None:
        organizations_branch = logic.get_action('group_tree')({}, {'type': 'organization'})
    for organization in organizations_branch:
        if organization['name'] == organization_name:
            if 'children' in organization and len(organization['children']) > 0:
                return collect_descendants(organization['children'])
        elif 'children' in organization and len(organization['children']) > 0:
            inner_search = search_organization(organization_name, organization['children'])
            if len(inner_search) > 0:
                return inner_search
    return []


def custom_organization_filter(organization_name):
    descendant_organizations = search_organization(organization_name)
    if descendant_organizations and len(descendant_organizations) > 0:
        descendant_organizations_filter = ' OR '.join(descendant_organizations)
        organization_filter = '(%s OR %s)' % (organization_name, descendant_organizations_filter)
    else:
        organization_filter = organization_name
    return ' organization:%s' % organization_filter


class GobArPackageController(PackageController):

    def search(self):
        package_type = self._guess_package_type()

        try:
            context = {'model': model, 'user': c.user or c.author, 'auth_user_obj': c.userobj}
            check_access('site_read', context)
        except NotAuthorized:
            abort(401, _('Not authorized to see this page'))

        q = c.q = request.params.get('q', u'')
        c.query_error = False
        page = self._get_page_number(request.params)
        limit = g.datasets_per_page
        params_nopage = [(k, v) for k, v in request.params.items() if k != 'page']

        def drill_down_url(alternative_url=None, **by):
            return h.add_url_param(
                alternative_url=alternative_url,
                controller='package',
                action='search',
                new_params=by
            )

        c.drill_down_url = drill_down_url

        def remove_field(key, value=None, replace=None):
            return h.remove_url_param(key, value=value, replace=replace, controller='package', action='search')

        c.remove_field = remove_field

        sort_by = request.params.get('sort', None)
        params_nosort = [(k, v) for k, v in params_nopage if k != 'sort']

        def _sort_by(fields):
            params = params_nosort[:]
            if fields:
                sort_string = ', '.join('%s %s' % f for f in fields)
                params.append(('sort', sort_string))
            return search_url(params, package_type)

        c.sort_by = _sort_by
        if not sort_by:
            c.sort_by_fields = []
        else:
            c.sort_by_fields = [field.split()[0] for field in sort_by.split(',')]

        def pager_url(q=None, page=None):
            params = list(params_nopage)
            params.append(('page', page))
            return search_url(params, package_type)

        c.search_url_params = urlencode(_encode_params(params_nopage))

        try:
            c.fields = []
            c.fields_grouped = {}
            search_extras = {}
            fq = ''
            for (param, value) in request.params.items():
                if param not in ['q', 'page', 'sort'] \
                        and len(value) and not param.startswith('_'):
                    if not param.startswith('ext_'):
                        c.fields.append((param, value))
                        if param != 'organization':
                            fq += ' %s:"%s"' % (param, value)
                        else:
                            fq += custom_organization_filter(value)
                        if param not in c.fields_grouped:
                            c.fields_grouped[param] = [value]
                        else:
                            c.fields_grouped[param].append(value)
                    else:
                        search_extras[param] = value

            context = {'model': model, 'session': model.Session,
                       'user': c.user or c.author, 'for_view': True,
                       'auth_user_obj': c.userobj}

            if package_type and package_type != 'dataset':
                fq += ' +dataset_type:{type}'.format(type=package_type)
            elif not asbool(config.get('ckan.search.show_all_types', 'False')):
                fq += ' +dataset_type:dataset'

            facets = OrderedDict()

            default_facet_titles = {
                'organization': _('Organizations'),
                'groups': _('Groups'),
                'tags': _('Tags'),
                'res_format': _('Formats'),
                'license_id': _('Licenses'),
            }

            for facet in g.facets:
                if facet in default_facet_titles:
                    facets[facet] = default_facet_titles[facet]
                else:
                    facets[facet] = facet

            for plugin in p.PluginImplementations(p.IFacets):
                facets = plugin.dataset_facets(facets, package_type)

            c.facet_titles = facets

            data_dict = {
                'q': q,
                'fq': fq.strip(),
                'facet.field': facets.keys(),
                'rows': limit,
                'start': (page - 1) * limit,
                'sort': sort_by,
                'extras': search_extras
            }

            query = get_action('package_search')(context, data_dict)
            c.sort_by_selected = query['sort']

            c.page = h.Page(
                collection=query['results'],
                page=page,
                url=pager_url,
                item_count=query['count'],
                items_per_page=limit
            )
            c.facets = query['facets']
            c.search_facets = query['search_facets']
            c.page.items = query['results']
        except SearchError, se:
            log.error('Dataset search error: %r', se.args)
            c.query_error = True
            c.facets = {}
            c.search_facets = {}
            c.page = h.Page(collection=[])
        c.search_facets_limits = {}
        for facet in c.search_facets.keys():
            try:
                if facet != 'organization':
                    limit = int(request.params.get('_%s_limit' % facet, g.facets_default_number))
                else:
                    limit = None
            except ValueError:
                error_description = _('Parameter "{parameter_name}" is not an integer')
                error_description = error_description.format(parameter_name='_%s_limit' % facet)
                abort(400, error_description)
            c.search_facets_limits[facet] = limit

        maintain.deprecate_context_item('facets', 'Use `c.search_facets` instead.')

        self._setup_template_variables(context, {}, package_type=package_type)

        return render(self._search_template(package_type), extra_vars={'dataset_type': package_type})

    def new(self, data=None, errors=None, error_summary=None):
        if data and 'type' in data:
            package_type = data['type']
        else:
            package_type = self._guess_package_type(True)

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj,
                   'save': 'save' in request.params}

        # Package needs to have a organization group in the call to
        # check_access and also to save it
        try:
            check_access('package_create', context)
        except NotAuthorized:
            abort(401, _('Unauthorized to create a package'))

        if context['save'] and not data:
            return self._save_new(context, package_type=package_type)

        data = data or clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(
            request.params, ignore_keys=CACHE_PARAMETERS))))
        c.resources_json = h.json.dumps(data.get('resources', []))
        # convert tags if not supplied in data
        if data and not data.get('tag_string'):
            data['tag_string'] = ', '.join(
                h.dict_list_reduce(data.get('tags', {}), 'name'))

        errors = errors or {}
        error_summary = error_summary or {}
        # in the phased add dataset we need to know that
        # we have already completed stage 1
        stage = ['active']
        if data.get('state', '').startswith('draft'):
            stage = ['active', 'complete']

        # if we are creating from a group then this allows the group to be
        # set automatically
        data['group_id'] = request.params.get('group') or \
                           request.params.get('groups__0__id')

        form_snippet = self._package_form(package_type=package_type)
        form_vars = {'data': data, 'errors': errors,
                     'error_summary': error_summary,
                     'action': 'new', 'stage': stage,
                     'dataset_type': package_type,
                     }
        c.errors_json = h.json.dumps(errors)

        self._setup_template_variables(context, {},
                                       package_type=package_type)

        new_template = self._new_template(package_type)
        c.form = deprecated_lazy_render(
            new_template,
            form_snippet,
            lambda: render(form_snippet, extra_vars=form_vars),
            'use of c.form is deprecated. please see '
            'ckan/templates/package/base_form_page.html for an example '
            'of the new way to include the form snippet'
        )
        return render(new_template,
                      extra_vars={'form_vars': form_vars, 'form_snippet': form_snippet, 'dataset_type': package_type})

    def _save_new(self, context, package_type=None):
        # The staged add dataset used the new functionality when the dataset is
        # partially created so we need to know if we actually are updating or
        # this is a real new.
        is_an_update = False
        ckan_phase = request.params.get('_ckan_phase')
        from ckan.lib.search import SearchIndexError
        try:
            data_dict = clean_dict(dict_fns.unflatten(
                tuplize_dict(parse_params(request.POST))))
            if ckan_phase:
                # prevent clearing of groups etc
                context['allow_partial_update'] = True
                # sort the tags
                if 'tag_string' in data_dict:
                    data_dict['tags'] = self._tag_string_to_list(data_dict['tag_string'])
                if data_dict.get('pkg_name'):
                    is_an_update = True
                    # This is actually an update not a save
                    data_dict['id'] = data_dict['pkg_name']
                    del data_dict['pkg_name']
                    # don't change the dataset state
                    data_dict['state'] = 'draft'
                    # this is actually an edit not a save
                    pkg_dict = get_action('package_update')(context, data_dict)

                    if request.params['save'] == 'go-metadata':
                        # redirect to add metadata
                        url = h.url_for(controller='package', action='new_metadata', id=pkg_dict['name'])
                    elif request.params['save'] == 'save-draft':
                        url = h.url_for(controller='package', action='read', id=pkg_dict['name'])
                    else:
                        # redirect to add dataset resources
                        url = h.url_for(controller='package', action='new_resource', id=pkg_dict['name'])
                    redirect(url)
                # Make sure we don't index this dataset
                if request.params['save'] not in ['go-resource', 'go-metadata']:
                    data_dict['state'] = 'draft'
                # allow the state to be changed
                context['allow_state_change'] = True

            data_dict['type'] = package_type
            context['message'] = data_dict.get('log_message', '')
            pkg_dict = get_action('package_create')(context, data_dict)

            if ckan_phase and request.params['save'] != 'save-draft':
                url = h.url_for(controller='package', action='new_resource', id=pkg_dict['name'])
                redirect(url)
            elif request.params['save'] == 'save-draft':
                url = h.url_for(controller='package', action='read', id=pkg_dict['name'])
                redirect(url)
            self._form_save_redirect(pkg_dict['name'], 'new', package_type=package_type)
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound, e:
            abort(404, _('Dataset not found'))
        except dict_fns.DataError:
            abort(400, _(u'Integrity Error'))
        except SearchIndexError, e:
            try:
                exc_str = unicode(repr(e.args))
            except Exception:  # We don't like bare excepts
                exc_str = unicode(str(e))
            abort(500, _(u'Unable to add package to search index.') + exc_str)
        except ValidationError, e:
            errors = e.error_dict
            error_summary = e.error_summary
            if is_an_update:
                # we need to get the state of the dataset to show the stage we
                # are on.
                pkg_dict = get_action('package_show')(context, data_dict)
                data_dict['state'] = pkg_dict['state']
                return self.edit(data_dict['id'], data_dict,
                                 errors, error_summary)
            data_dict['state'] = 'none'
            return self.new(data_dict, errors, error_summary)
