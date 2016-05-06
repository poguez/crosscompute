import os
import re
import shlex
import subprocess
import sys
import time
from collections import OrderedDict
from os import sep
from os.path import abspath, isabs, join
from invisibleroads.scripts import (
    StoicArgumentParser, configure_subparsers, get_scripts_by_name,
    run_scripts)
from invisibleroads_macros.configuration import RawCaseSensitiveConfigParser
from invisibleroads_macros.disk import cd
from invisibleroads_macros.exceptions import InvisibleRoadsError
from invisibleroads_macros.log import (
    format_hanging_indent, format_path, format_summary,
    parse_nested_dictionary_from, sort_dictionary, stylize_dictionary)
from invisibleroads_repositories import (
    get_github_repository_commit_hash, get_github_repository_url)

from ..configurations import get_tool_definition
from ..exceptions import CrossComputeError
from ..types import parse_data_dictionary


class _ResultConfiguration(object):

    def __init__(self, target_folder):
        self.target_folder = target_folder
        self.target_file = open(join(target_folder, 'result.cfg'), 'wt')

    def write(self, screen_text, file_text=None):
        _write(self.target_file, screen_text, file_text)

    def write_header(self, tool_definition, result_arguments):
        # Get values before they are removed from tool_definition
        configuration_folder = tool_definition['configuration_folder']
        tool_argument_names = list(tool_definition['argument_names'])
        # Write tool_definition
        template = '[tool_definition]\n%s'
        command_path = self.write_script(tool_definition, result_arguments)
        tool_definition = stylize_tool_definition(tool_definition)
        self.write(template % format_summary(sort_dictionary(tool_definition, [
            'repository_url', 'tool_name', 'commit_hash', 'configuration_path',
        ])))
        print(format_summary({'command_path': command_path}) + '\n')
        # Put target_folder at end of result_arguments
        target_folder = result_arguments['target_folder']
        try:
            tool_argument_names.remove('target_folder')
        except ValueError:
            pass
        result_arguments = sort_dictionary(
            result_arguments, tool_argument_names)
        # Write result_arguments
        template = '[result_arguments]\n%s\n'
        result_arguments['target_folder'] = target_folder
        for k, v in result_arguments.items():
            if not k.endswith('_path') or isabs(v):
                continue
            result_arguments[k] = abspath(join(configuration_folder, v))
        self.write(template % format_summary(result_arguments))

    def write_script(self, tool_definition, result_arguments):
        configuration_folder = tool_definition['configuration_folder']
        if os.name == 'posix':
            line_join = '\\'
            script_name = 'run.sh'
            script_header = '\n'.join([
                'CONFIGURATION_FOLDER=' + format_path(configuration_folder),
                'cd ${CONFIGURATION_FOLDER}'
            ])
        else:
            line_join = '^'
            script_name = 'run.bat'
            script_header = '\n'.join([
                'SET CONFIGURATION_FOLDER=' + format_path(configuration_folder),
                'cd %CONFIGURATION_FOLDER%'
            ])
        script_path = join(self.target_folder, script_name)
        command = render_command(
            tool_definition['command_template'],
            stylize_dictionary(result_arguments, [
                ('_folder', format_path),
                ('_path', format_path),
            ]))
        with open(script_path, 'wt') as script_file:
            script_file.write(script_header + '\n')
            script_file.write(format_hanging_indent(
                command.replace('\n', ' %s\n' % line_join)) + '\n')
        return script_path

    def write_footer(self, result_properties):
        template = '[result_properties]\n%s'
        self.write(
            screen_text=template % format_summary(
                result_properties, censored=False),
            file_text=template % format_summary(
                result_properties, censored=True))


def launch(argv=sys.argv):
    argument_parser = StoicArgumentParser('crosscompute', add_help=False)
    argument_subparsers = argument_parser.add_subparsers(dest='command')
    scripts_by_name = get_scripts_by_name('crosscompute')
    configure_subparsers(argument_subparsers, scripts_by_name)
    args = argument_parser.parse_known_args(argv[1:])[0]
    run_scripts(scripts_by_name, args)


def load_tool_definition(tool_name):
    if tool_name:
        tool_name = tool_name.rstrip(sep)  # Remove folder autocompletion slash
    try:
        tool_definition = get_tool_definition(tool_name=tool_name)
    except CrossComputeError as e:
        sys.exit(e)
    return tool_definition


def load_result_configuration(result_folder):
    result_configuration = RawCaseSensitiveConfigParser()
    result_configuration.read(join(result_folder, 'result.cfg'))
    result_arguments = OrderedDict(
        result_configuration.items('result_arguments'))
    result_properties = parse_nested_dictionary_from(OrderedDict(
        result_configuration.items('result_properties')), max_depth=1)
    return result_arguments, result_properties


def stylize_tool_definition(tool_definition):
    d = {
        'tool_name': tool_definition['tool_name'],
        'configuration_path': tool_definition['configuration_path'],
    }
    configuration_folder = tool_definition['configuration_folder']
    try:
        d['repository_url'] = get_github_repository_url(
            configuration_folder)
        d['commit_hash'] = get_github_repository_commit_hash(
            configuration_folder)
    except InvisibleRoadsError:
        pass
    return d


def run_script(
        target_folder, tool_definition, result_arguments, data_type_by_suffix):
    result_properties, timestamp = OrderedDict(), time.time()
    result_arguments = dict(result_arguments, target_folder=target_folder)
    result_configuration = _ResultConfiguration(target_folder)
    result_configuration.write_header(tool_definition, result_arguments)
    command = render_command(tool_definition[
        'command_template'], result_arguments).replace('\n', ' ')
    try:
        with cd(tool_definition['configuration_folder']):
            command_process = subprocess.Popen(
                shlex.split(command, posix=os.name == 'posix'),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        standard_output, standard_error = [
            x.rstrip().decode('utf-8') for x in command_process.communicate()]
        if command_process.returncode:
            result_properties['return_code'] = command_process.returncode
    except OSError:
        standard_output, standard_error = None, 'Command not found'
    result_properties.update(_process_streams(
        standard_output, standard_error, target_folder, tool_definition,
        data_type_by_suffix))
    result_properties['execution_time_in_seconds'] = time.time() - timestamp
    result_configuration.write_footer(result_properties)
    return result_properties


def render_command(command_template, result_arguments):
    d = {}
    quote_pattern = re.compile(r"""["'].*["']""")
    for k, v in result_arguments.items():
        v = str(v).strip()
        if ' ' in v and not quote_pattern.match(v):
            v = '"%s"' % v
        d[k] = v
    return command_template.format(**d)


def _process_streams(
        standard_output, standard_error, target_folder, tool_definition,
        data_type_by_suffix):
    d, type_errors = OrderedDict(), OrderedDict()
    for stream_name, stream_content in [
        ('standard_output', standard_output),
        ('standard_error', standard_error),
    ]:
        if not stream_content:
            continue
        _write(
            open(join(target_folder, '%s.log' % stream_name), 'wt'),
            screen_text='[%s]\n%s\n' % (stream_name, stream_content),
            file_text=stream_content)
        value_by_key, errors = parse_data_dictionary(
            stream_content, data_type_by_suffix, target_folder)
        for k, v in errors:
            type_errors['%s.error' % k] = v
        if tool_definition.get('show_' + stream_name):
            d[stream_name] = stream_content
        if value_by_key:
            d[stream_name + 's'] = value_by_key
    if type_errors:
        d['type_errors'] = type_errors
    return d


def _write(target_file, screen_text, file_text=None):
    if not file_text:
        file_text = screen_text
    print(screen_text.encode('utf-8'))
    target_file.write(file_text.encode('utf-8') + '\n')
